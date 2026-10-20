"""Realtime Claude curator (Haiku 4.5 micro-batches, streamed verdicts).

Per batch: one streaming Messages API call. System = frozen RUBRIC (cached)
+ goals block (cached). Output is JSON {"verdicts": [...]} constrained by
structured outputs (output_config.format) or, with structured_output=False,
a single forced tool call. Text / partial-JSON deltas are fed to an
incremental parser that yields each verdict object as soon as its closing
brace arrives.

Never raises for model problems: timeout, API error, malformed JSON, unknown
or missing ids -> HeuristicCurator verdicts with stage="fallback". All
verdicts pass through apply_policy.

Metadata (for CuratedPost.curator):
    curator.meta_for(post_id) -> {backend, model, goals_hash, batch_id,
                                  latency_ms, stage}
        latency_ms = ms from batch request start to that verdict's yield.
        Kept in a bounded LRU (meta_cache_size ids).
    curator.last_meta -> summary of the most recently finished batch:
        {batch_id, kind, n, n_model, n_fallback, first_verdict_ms, total_ms,
         error, stop_reason, usage{input_tokens, output_tokens,
         cache_read_input_tokens, cache_creation_input_tokens}}
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator, Optional

from src.models import Post
from src.stream.contracts import CuratorCaps, CuratorContext, Verdict

from . import apply_policy, post_to_dict, prefilter_verdict
from .heuristic import HeuristicCurator
from .prompt import (
    CATEGORIES,
    FLAGS,
    VERDICTS_SCHEMA,
    posts_user_message,
    replies_user_message,
    system_blocks,
)

log = logging.getLogger(__name__)

TOOL_NAME = "submit_verdicts"


class VerdictStreamParser:
    """Incremental scanner over a JSON text stream. Emits every object whose
    opening brace sits directly inside an array that is the first array
    encountered (covers {"verdicts":[{...}]} and a bare [{...}]). Tolerates
    leading prose / code fences. Malformed objects are skipped."""

    def __init__(self):
        self._stack: list[str] = []
        self._in_str = False
        self._esc = False
        self._buf: list[str] = []
        self._capturing = False
        self._cap_depth = 0
        self._array_depth: Optional[int] = None  # stack depth of the verdict array
        self.errors = 0

    def feed(self, chunk: str) -> list[dict]:
        out: list[dict] = []
        for ch in chunk:
            if self._capturing:
                self._buf.append(ch)
            if self._in_str:
                if self._esc:
                    self._esc = False
                elif ch == "\\":
                    self._esc = True
                elif ch == '"':
                    self._in_str = False
                continue
            if ch == '"':
                if self._stack:
                    self._in_str = True
                continue
            if ch in "{[":
                if ch == "[" and self._array_depth is None:
                    self._array_depth = len(self._stack) + 1
                self._stack.append(ch)
                if (ch == "{" and not self._capturing and self._array_depth is not None
                        and len(self._stack) == self._array_depth + 1):
                    self._capturing = True
                    self._cap_depth = len(self._stack)
                    self._buf = ["{"]
            elif ch in "}]":
                if not self._stack:
                    continue
                self._stack.pop()
                if self._capturing and ch == "}" and len(self._stack) == self._cap_depth - 1:
                    self._capturing = False
                    try:
                        obj = json.loads("".join(self._buf))
                        if isinstance(obj, dict):
                            out.append(obj)
                    except ValueError:
                        self.errors += 1
                    self._buf = []
        return out


def _clip_words(s: str, n: int = 15) -> str:
    return " ".join(str(s).split()[:n])


def coerce_verdict(obj: dict, pending: set[str]) -> Optional[Verdict]:
    """Validate/clamp one model object. None if unusable or id not pending."""
    try:
        vid = str(obj.get("id", ""))
        if vid not in pending:
            return None
        score = int(round(float(obj["score"])))
        cat = str(obj.get("category", "neutral"))
        if cat not in CATEGORIES:
            cat = "neutral"
        flags = [f for f in (obj.get("flags") or []) if f in FLAGS]
        conf = obj.get("confidence", 0.7)
        conf = 0.7 if not isinstance(conf, (int, float)) else max(0.0, min(1.0, float(conf)))
        return Verdict(id=vid, score=max(0, min(100, score)), category=cat,
                       reason=_clip_words(obj.get("reason", "")), confidence=conf,
                       flags=list(dict.fromkeys(flags)), stage="model")
    except (KeyError, TypeError, ValueError):
        return None


class ClaudeCurator:
    name = "claude"

    def __init__(
        self,
        model: str = "claude-haiku-4-5",
        client: Any = None,
        timeout_s: float = 4.0,
        max_in_flight: int = 2,
        *,
        max_batch: int = 8,
        structured_output: bool = True,
        fallback: Optional[HeuristicCurator] = None,
        meta_cache_size: int = 4096,
    ):
        self.model = model
        self._client = client
        self.timeout_s = timeout_s
        self.structured_output = structured_output
        self.caps = CuratorCaps(max_batch=max_batch, streaming=True, latency_class="realtime")
        self.fallback = fallback or HeuristicCurator()
        self._sem = asyncio.Semaphore(max(1, max_in_flight))
        self._meta: OrderedDict[str, dict] = OrderedDict()
        self._meta_cap = meta_cache_size
        self.last_meta: dict = {}

    # -- client / request -----------------------------------------------

    @property
    def client(self):
        if self._client is None:
            import anthropic
            # Realtime: no SDK retries (we fall back instead); timeout is ours.
            self._client = anthropic.AsyncAnthropic(max_retries=0)
        return self._client

    def build_request(self, ctx: CuratorContext, user_text: str, n_items: int) -> dict:
        """Request kwargs (without stream=True). Public for tests/inspection."""
        req: dict = {
            "model": self.model,
            "max_tokens": min(4096, 256 + 120 * n_items),
            "system": system_blocks(ctx.goals_text),
            "messages": [{"role": "user", "content": user_text}],
        }
        if self.structured_output:
            req["output_config"] = {"format": {"type": "json_schema", "schema": VERDICTS_SCHEMA}}
        else:
            req["tools"] = [{
                "name": TOOL_NAME,
                "description": "Submit one verdict per input id.",
                "input_schema": VERDICTS_SCHEMA,
                "eager_input_streaming": True,
            }]
            req["tool_choice"] = {"type": "tool", "name": TOOL_NAME}
        return req

    async def warm(self, ctx: CuratorContext) -> dict:
        """Prime the prompt cache for ctx (call at session start / goals change).
        max_tokens=0 is rejected with output_config.format / forced tools, so
        this uses max_tokens=1 with the same prefix. Returns usage dict;
        never raises."""
        req = self.build_request(ctx, "warmup", 1)
        req["max_tokens"] = 1
        try:
            resp = await self.client.messages.create(**req, timeout=max(self.timeout_s, 10.0))
            return _usage_dict(getattr(resp, "usage", None))
        except Exception as e:  # noqa: BLE001
            log.warning("curator warm failed: %s", e)
            return {"error": repr(e)}

    async def _stream_objects(self, req: dict, info: dict) -> AsyncIterator[dict]:
        stream = await self.client.messages.create(**req, stream=True, timeout=self.timeout_s + 1.0)
        parser = VerdictStreamParser()
        try:
            async for event in stream:
                et = getattr(event, "type", None)
                if et == "message_start":
                    info["usage"].update(_usage_dict(getattr(event.message, "usage", None)))
                elif et == "content_block_delta":
                    d = event.delta
                    dt = getattr(d, "type", None)
                    chunk = d.text if dt == "text_delta" else d.partial_json if dt == "input_json_delta" else ""
                    for obj in parser.feed(chunk):
                        yield obj
                elif et == "message_delta":
                    info["stop_reason"] = getattr(event.delta, "stop_reason", None)
                    u = _usage_dict(getattr(event, "usage", None))
                    if u.get("output_tokens") is not None:
                        info["usage"]["output_tokens"] = u["output_tokens"]
        finally:
            info["parse_errors"] = parser.errors
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    r = close()
                    if inspect.isawaitable(r):
                        await r
                except Exception:  # noqa: BLE001
                    pass

    # -- batch engine ----------------------------------------------------

    def _record(self, vid: str, meta: dict) -> None:
        self._meta[vid] = meta
        self._meta.move_to_end(vid)
        while len(self._meta) > self._meta_cap:
            self._meta.popitem(last=False)

    def meta_for(self, verdict_id: str) -> dict:
        return dict(self._meta.get(verdict_id, {}))

    async def _run_batch(self, kind: str, items: list[dict], user_text: str, ctx: CuratorContext,
                         fallback_fn) -> AsyncIterator[Verdict]:
        batch_id = uuid.uuid4().hex[:12]
        pending = {str(i.get("id", "")) for i in items}
        info: dict = {"usage": {}, "stop_reason": None, "error": None}
        n_model = 0
        first_ms: Optional[int] = None
        t0 = time.monotonic()

        def meta(stage: str) -> dict:
            return {"backend": self.name, "model": self.model, "goals_hash": ctx.goals_hash,
                    "batch_id": batch_id, "latency_ms": int((time.monotonic() - t0) * 1000),
                    "stage": stage}

        async with self._sem:
            t0 = time.monotonic()  # deadline starts once we hold a slot
            deadline = t0 + self.timeout_s
            gen = self._stream_objects(self.build_request(ctx, user_text, len(items)), info)
            try:
                while pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    try:
                        obj = await asyncio.wait_for(gen.__anext__(), remaining)
                    except StopAsyncIteration:
                        break
                    v = coerce_verdict(obj, pending)
                    if v is None:
                        continue
                    pending.discard(v.id)
                    n_model += 1
                    m = meta("model")
                    if first_ms is None:
                        first_ms = m["latency_ms"]
                    self._record(v.id, m)
                    yield apply_policy(v, ctx)
            except asyncio.TimeoutError:
                info["error"] = f"timeout after {self.timeout_s}s"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — API/network/anything: fall back
                info["error"] = f"{type(e).__name__}: {e}"
            finally:
                try:
                    await gen.aclose()
                except BaseException:  # noqa: BLE001
                    pass

        if pending and info["error"] is None:
            info["error"] = f"missing {len(pending)} id(s) (stop_reason={info['stop_reason']})"
        if info["error"]:
            log.info("curator batch %s fallback: %s", batch_id, info["error"])
        n_fb = 0
        for item in items:
            iid = str(item.get("id", ""))
            if iid in pending:
                pending.discard(iid)
                v = fallback_fn(item)
                v.stage = "fallback"
                n_fb += 1
                self._record(iid, meta("fallback"))
                yield apply_policy(v, ctx)

        self.last_meta = {
            "batch_id": batch_id, "kind": kind, "n": len(items), "n_model": n_model,
            "n_fallback": n_fb, "first_verdict_ms": first_ms,
            "total_ms": int((time.monotonic() - t0) * 1000), "error": info["error"],
            "stop_reason": info["stop_reason"], "usage": info["usage"],
            "parse_errors": info.get("parse_errors", 0),
        }

    async def _merge(self, gens: list[AsyncIterator[Verdict]]) -> AsyncIterator[Verdict]:
        if len(gens) == 1:
            async for v in gens[0]:
                yield v
            return
        q: asyncio.Queue = asyncio.Queue()
        done = object()

        async def pump(g):
            try:
                async for v in g:
                    await q.put(v)
            finally:
                await q.put(done)

        tasks = [asyncio.create_task(pump(g)) for g in gens]
        try:
            remaining = len(tasks)
            while remaining:
                item = await q.get()
                if item is done:
                    remaining -= 1
                else:
                    yield item
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- Curator protocol ------------------------------------------------

    async def curate(self, posts: list[Post], ctx: CuratorContext) -> AsyncIterator[Verdict]:
        to_model: list[dict] = []
        for post in posts:
            pre = prefilter_verdict(post)
            if pre is not None:
                self._record(pre.id, {"backend": self.name, "model": None, "goals_hash": ctx.goals_hash,
                                      "batch_id": None, "latency_ms": 0, "stage": "prefilter"})
                yield apply_policy(pre, ctx)
            else:
                to_model.append(post_to_dict(post))
        if not to_model:
            return
        n = self.caps.max_batch
        fb = lambda item: self.fallback.score_post(item, ctx)  # noqa: E731
        gens = [self._run_batch("posts", chunk, posts_user_message(chunk), ctx, fb)
                for chunk in (to_model[i:i + n] for i in range(0, len(to_model), n))]
        async for v in self._merge(gens):
            yield v

    async def curate_replies(self, parent: Post, replies: list[dict], ctx: CuratorContext) -> AsyncIterator[Verdict]:
        if not replies:
            return
        pdict = post_to_dict(parent)
        n = max(self.caps.max_batch, 16)  # replies are short; bigger batches are fine
        fb = lambda item: self.fallback.score_reply(item, pdict, ctx)  # noqa: E731
        gens = [self._run_batch("replies", chunk, replies_user_message(pdict, chunk), ctx, fb)
                for chunk in (replies[i:i + n] for i in range(0, len(replies), n))]
        async for v in self._merge(gens):
            yield v


def _usage_dict(usage) -> dict:
    if usage is None:
        return {}
    out = {}
    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        v = usage.get(k) if isinstance(usage, dict) else getattr(usage, k, None)
        if v is not None:
            out[k] = v
    return out

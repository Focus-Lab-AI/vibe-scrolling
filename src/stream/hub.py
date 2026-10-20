"""StreamHub — session orchestration for the streaming host.

Pipeline:

  producers ──raw_queue──► prefilter (id/text dedupe, ads, empty) ──cur_queue──►
  curation (micro-batches, ≤2 in flight, policy) ──► ready buffer (boost first)
  ──► subscribers (phones; broadcast) ; every verdict → EventLog

Demand: level = min phone buffer depth (reported + sent since report)
+ ready + in-pipeline. Producers scroll while level is below the high
watermark and resume once it falls to the low watermark.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import re
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from src.models import Post
from src.stream.contracts import (
    CuratedPost,
    CuratedReply,
    CuratedThread,
    Curator,
    CuratorContext,
    PlatformAdapter,
    PlatformStatus,
    Verdict,
    to_wire,
)
from src.stream.curator import apply_policy, prefilter_verdict
from src.stream.event_log import EventLog, write_filtered as _write_filtered
from src.stream.producer import DemandSignal, FeedProducer, ProducerConfig

COLLAPSE_FLAGS = {"rage_bait", "engagement_bait"}


@dataclass
class HubConfig:
    low_watermark: int = 8
    high_watermark: int = 20
    batch_window: float = 1.5
    max_inflight: int = 2
    max_batch: Optional[int] = None  # None → curator.caps.max_batch
    default_buffer_target: int = 20
    text_dedupe_min_chars: int = 20
    comments_limit: int = 50
    host_mode: str = "server"  # "laptop": the feed browsers are visible windows on the host
    login_hint: str = ""
    producer: ProducerConfig = field(default_factory=ProducerConfig)
    producer_overrides: dict[str, ProducerConfig] = field(default_factory=dict)


class Subscriber:
    """One connected phone. The WS layer drains `queue` into the socket."""

    _ids = itertools.count(1)

    def __init__(self, buffer_target: int = 20, debug: bool = False):
        self.id = next(self._ids)
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self.buffer_target = buffer_target
        self.debug = debug
        self.reported_depth = 0
        self.sent_since_report = 0
        self.last_seen_post_id: Optional[str] = None
        self.platforms: Optional[list[str]] = None

    @property
    def depth(self) -> int:
        return self.reported_depth + self.sent_since_report

    def room(self) -> int:
        return self.buffer_target - self.depth

    def send(self, msg: dict) -> None:
        self.queue.put_nowait(msg)


def _norm_text_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _default_ctx() -> CuratorContext:
    return CuratorContext(goals_text="", goals_hash=hashlib.sha256(b"").hexdigest())


def describe_drop_rule(threshold: Optional[int]) -> str:
    if threshold is None:
        return "category=drain"
    if threshold < 0:
        return "keep everything"
    return f"category=drain OR score<={threshold}"


class StreamHub:
    def __init__(
        self,
        pool,
        adapters: dict[str, PlatformAdapter],
        curator: Curator,
        *,
        ctx: Optional[CuratorContext] = None,
        config: Optional[HubConfig] = None,
        streams_dir: Optional[Path | str] = None,
        workspace_dir: Optional[Path | str] = None,
        session_id: Optional[str] = None,
    ):
        self.pool = pool
        self.adapters = dict(adapters)
        self.curator = curator
        self.ctx = ctx or _default_ctx()
        self.config = config or HubConfig()
        self.workspace_dir = Path(workspace_dir) if workspace_dir else None
        self.session_id = session_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.session_dir: Optional[Path] = (
            Path(streams_dir) / self.session_id if streams_dir else None
        )
        self.event_log: Optional[EventLog] = EventLog(self.session_dir) if self.session_dir else None

        self.demand = DemandSignal(self.config.low_watermark, self.config.high_watermark)
        self.raw_queue: asyncio.Queue[Post] = asyncio.Queue()
        self.cur_queue: asyncio.Queue[Post] = asyncio.Queue()
        self.producers: dict[str, FeedProducer] = {}
        self.subscribers: list[Subscriber] = []
        self.ready: deque[dict] = deque()
        self.ready_boost: deque[dict] = deque()

        self.posts_by_id: dict[str, Post] = {}
        self._seen_keys: set[str] = set()
        self._text_hashes: dict[str, str] = {}
        self._intercepted_at: dict[str, float] = {}
        self._seq = itertools.count(1)
        self._batch_ids = itertools.count(1)
        self._inflight_posts = 0
        self._inflight_sem = asyncio.Semaphore(self.config.max_inflight)
        self._wake_curation = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._batch_tasks: set[asyncio.Task] = set()
        self._comment_locks: dict[str, asyncio.Lock] = {}
        self.latency_ms: Optional[float] = None
        self.counters = {"prefilter_dropped": 0, "dup_id": 0, "dup_text": 0, "delivered": 0}
        self.started = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self, platforms: Optional[Iterable[str]] = None) -> None:
        if not self.started:
            self.started = True
            self._tasks = [
                asyncio.create_task(self._prefilter_loop(), name="hub:prefilter"),
                asyncio.create_task(self._curation_loop(), name="hub:curation"),
            ]
            print(f"[stream] hub session {self.session_id} started"
                  + (f", log {self.session_dir}" if self.session_dir else ""))
        await self.ensure_platforms(platforms)

    async def ensure_platforms(self, platforms: Optional[Iterable[str]] = None) -> list[str]:
        wanted = list(self.adapters) if platforms is None else list(platforms)
        started = []
        for name in wanted:
            adapter = self.adapters.get(name)
            if adapter is None:
                continue
            prod = self.producers.get(name)
            if prod is None:
                cfg = self.config.producer_overrides.get(name, self.config.producer)
                prod = FeedProducer(
                    name, adapter, self.pool, self.raw_queue, self.demand,
                    config=cfg, on_status=self._on_producer_status,
                )
                self.producers[name] = prod
            if not prod.running:  # first hello, or stopped / errored / session cap
                prod.start()
            elif prod.needs_login:  # a phone just (re)connected: check again now
                prod.recheck_login()
            started.append(name)
        return started

    async def close(self, close_pool: bool = False) -> None:
        for prod in list(self.producers.values()):
            await prod.stop()
        for t in self._tasks + list(self._batch_tasks):
            t.cancel()
        for t in self._tasks + list(self._batch_tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        self.started = False
        if self.event_log:
            self.event_log.close()
        if close_pool and self.pool is not None:
            await self.pool.close()

    # ------------------------------------------------------------------ subscribers
    def subscribe(
        self,
        buffer_target: Optional[int] = None,
        debug: bool = False,
        initial: Optional[list[dict]] = None,
    ) -> Subscriber:
        """Register a phone. `initial` messages are queued before any post."""
        sub = Subscriber(buffer_target or self.config.default_buffer_target, debug=debug)
        for msg in initial or []:
            sub.send(msg)
        self.subscribers.append(sub)
        self._pump()
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        if sub in self.subscribers:
            self.subscribers.remove(sub)
        self._recompute_demand()

    def update_demand(
        self, sub: Subscriber, buffer_depth: int, last_seen_post_id: Optional[str] = None
    ) -> None:
        sub.reported_depth = max(0, int(buffer_depth))
        sub.sent_since_report = 0
        if last_seen_post_id:
            sub.last_seen_post_id = last_seen_post_id
        self._pump()
        self._recompute_demand()

    def broadcast(self, msg: dict) -> None:
        for sub in self.subscribers:
            sub.send(msg)

    def _phone_depth(self) -> int:
        if not self.subscribers:
            return 0
        return min(s.depth for s in self.subscribers)

    def pending(self) -> int:
        return self.raw_queue.qsize() + self.cur_queue.qsize() + self._inflight_posts

    def ready_count(self) -> int:
        return len(self.ready) + len(self.ready_boost)

    def _recompute_demand(self) -> None:
        level = self._phone_depth() + self.ready_count() + self.pending()
        self.demand.update(level)

    def _urgent(self) -> bool:
        return self._phone_depth() + self.ready_count() < self.config.low_watermark

    def _pump(self) -> None:
        """Move ready posts to phones while the hungriest phone has room."""
        while self.ready_count() and any(s.room() > 0 for s in self.subscribers):
            ev = self.ready_boost.popleft() if self.ready_boost else self.ready.popleft()
            for sub in self.subscribers:
                sub.send(ev)
                sub.sent_since_report += 1
            self.counters["delivered"] += 1
        self._recompute_demand()

    # ------------------------------------------------------------------ status
    def _on_producer_status(self, status: PlatformStatus) -> None:
        self.broadcast(self.status_message())
        if self.event_log:
            self.event_log.append({"type": "producer.status", "ts": time.time(), **asdict(status)})

    def platform_statuses(self) -> list[PlatformStatus]:
        return [p.status for p in self.producers.values()]

    def status_message(self) -> dict:
        return {
            "type": "status",
            "session_id": self.session_id,
            "platforms": [asdict(s) for s in self.platform_statuses()],
            "buffer": {
                "ready": self.ready_count(),
                "pending": self.pending(),
                "phone_depth": self._phone_depth(),
                "level": self.demand.level,
                "wanting": self.demand.wanting,
                "low": self.demand.low,
                "high": self.demand.high,
            },
            "curator": {
                "backend": getattr(self.curator, "name", type(self.curator).__name__),
                "latency_ms": round(self.latency_ms) if self.latency_ms is not None else None,
            },
            "counters": dict(self.counters),
            "subscribers": len(self.subscribers),
            "ts": time.time(),
        }

    def session_message(self) -> dict:
        def public_state(state: str) -> str:
            return state if state in ("needs_login", "error") else "ready"

        return {
            "type": "session",
            "session_id": self.session_id,
            "platforms": [
                {"platform": s.platform, "status": public_state(s.state), "state": s.state, "detail": s.detail}
                for s in self.platform_statuses()
            ],
            "host": {"mode": self.config.host_mode, "login_hint": self.config.login_hint},
            "ts": time.time(),
        }

    # ------------------------------------------------------------------ prefilter
    async def _prefilter_loop(self) -> None:
        while True:
            post = await self.raw_queue.get()
            try:
                self._prefilter(post)
            except Exception as e:  # noqa: BLE001
                print(f"[stream] prefilter error on {post.platform}:{post.id}: {e}")
            self._recompute_demand()

    def _prefilter(self, post: Post) -> None:
        key = f"{post.platform}:{post.id}"
        if key in self._seen_keys:
            self.counters["dup_id"] += 1
            return
        self._seen_keys.add(key)
        self.posts_by_id[post.id] = post
        self.posts_by_id[key] = post
        self._intercepted_at[key] = time.time()

        text = (post.text or "").strip()
        if len(text) >= self.config.text_dedupe_min_chars:
            h = _norm_text_hash(text)
            first = self._text_hashes.get(h)
            if first is not None:
                self.counters["dup_text"] += 1
                v = Verdict(id=post.id, score=0, category="neutral",
                            reason=f"Duplicate of {first}.", flags=["duplicate"],
                            action="drop", stage="prefilter")
                self._emit(post, v, batch_id="prefilter", latency_ms=0)
                return
            self._text_hashes[h] = key

        pv = prefilter_verdict(post)
        if pv is not None:
            pv = apply_policy(pv, self.ctx)
            if pv.action == "drop":
                self.counters["prefilter_dropped"] += 1
            self._emit(post, pv, batch_id="prefilter", latency_ms=0)
            return
        self.cur_queue.put_nowait(post)
        self._wake_curation.set()

    # ------------------------------------------------------------------ curation
    def _max_batch(self) -> int:
        caps = getattr(self.curator, "caps", None)
        return max(1, self.config.max_batch or getattr(caps, "max_batch", 8) or 8)

    async def _collect_batch(self) -> list[Post]:
        batch = [await self.cur_queue.get()]
        max_batch = self._max_batch()
        deadline = time.monotonic() + self.config.batch_window
        while len(batch) < max_batch:
            while len(batch) < max_batch and not self.cur_queue.empty():
                batch.append(self.cur_queue.get_nowait())
            if len(batch) >= max_batch or self._urgent():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(self.cur_queue.get(), min(remaining, 0.1)))
            except asyncio.TimeoutError:
                pass
        return batch

    async def _curation_loop(self) -> None:
        while True:
            await self._inflight_sem.acquire()
            try:
                batch = await self._collect_batch()
            except BaseException:
                self._inflight_sem.release()
                raise
            self._inflight_posts += len(batch)
            task = asyncio.create_task(self._curate_batch(batch))
            self._batch_tasks.add(task)
            task.add_done_callback(self._batch_tasks.discard)

    async def _curate_batch(self, batch: list[Post]) -> None:
        batch_id = f"b{next(self._batch_ids)}"
        t0 = time.monotonic()
        by_id = {p.id: p for p in batch}
        done: set[str] = set()
        try:
            async for v in self.curator.curate(batch, self.ctx):
                post = by_id.get(v.id)
                if post is None or v.id in done:
                    continue
                done.add(v.id)
                latency = (time.monotonic() - t0) * 1000
                self._inflight_posts -= 1
                self._emit(post, apply_policy(v, self.ctx), batch_id=batch_id, latency_ms=latency)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[stream] curator {getattr(self.curator, 'name', '?')} failed on {batch_id}: {e}")
        finally:
            latency = (time.monotonic() - t0) * 1000
            for pid, post in by_id.items():
                if pid in done:
                    continue
                self._inflight_posts -= 1
                v = Verdict(id=pid, score=50, category="neutral", reason="Curator gave no verdict.",
                            confidence=0.0, stage="fallback")
                self._emit(post, apply_policy(v, self.ctx), batch_id=batch_id, latency_ms=latency)
            self.latency_ms = latency if self.latency_ms is None else 0.7 * self.latency_ms + 0.3 * latency
            self._inflight_sem.release()
            self._recompute_demand()

    # ------------------------------------------------------------------ emit
    def _emit(self, post: Post, verdict: Verdict, *, batch_id: str, latency_ms: float) -> None:
        key = f"{post.platform}:{post.id}"
        ev = CuratedPost(
            post=asdict(post),
            verdict=asdict(verdict),
            curator={
                "backend": getattr(self.curator, "name", type(self.curator).__name__),
                "model": getattr(self.curator, "model", None),
                "goals_hash": self.ctx.goals_hash,
                "batch_id": batch_id,
                "latency_ms": round(latency_ms),
            },
            platform=post.platform,
            seq=next(self._seq),
            session_id=self.session_id,
            intercepted_at=self._intercepted_at.get(key, time.time()),
        )
        wire = to_wire(ev)
        if self.event_log:
            self.event_log.append(wire)
        prod = self.producers.get(post.platform)
        if verdict.action == "drop":
            if prod:
                prod.status.dropped += 1
            for sub in self.subscribers:
                if sub.debug:
                    sub.send(wire)
            return
        if prod:
            prod.status.kept += 1
        (self.ready_boost if verdict.action == "boost" else self.ready).append(wire)
        self._pump()

    # ------------------------------------------------------------------ interactions
    def find_post(self, post_id: str) -> Optional[Post]:
        return self.posts_by_id.get(post_id)

    async def open_comments(self, post_id: str, request_id: Optional[str] = None) -> CuratedThread:
        request_id = request_id or uuid.uuid4().hex
        post = self.find_post(post_id)
        if post is None:
            raise KeyError(f"unknown post {post_id}")
        adapter = self.adapters.get(post.platform)
        if adapter is None:
            raise KeyError(f"no adapter for platform {post.platform}")
        lock = self._comment_locks.setdefault(post.platform, asyncio.Lock())
        async with lock:  # one detail fetch per platform at a time
            context = await self.pool.get_context(post.platform)
            replies = await adapter.fetch_comments(context, post, limit=self.config.comments_limit)
        verdicts: dict[str, Verdict] = {}
        if replies:
            try:
                async for v in self.curator.curate_replies(post, replies, self.ctx):
                    verdicts[v.id] = apply_policy(v, self.ctx)
            except Exception as e:  # noqa: BLE001
                print(f"[stream] reply curation failed for {post_id}: {e}")
        out = []
        for r in replies:
            v = verdicts.get(str(r.get("id")))
            if v is None:
                out.append(asdict(CuratedReply(reply=r, action="show", reason="not curated")))
                continue
            if v.action == "drop":
                action = "hide"
            elif COLLAPSE_FLAGS & set(v.flags):
                action = "collapse"
            else:
                action = "show"
            out.append(asdict(CuratedReply(reply=r, action=action, flags=list(v.flags), reason=v.reason)))
        thread = CuratedThread(request_id=request_id, post_id=post_id, replies=out)
        if self.event_log:
            self.event_log.append(to_wire(thread))
        return thread

    def record(self, event: dict) -> None:
        """Log a client event (e.g. feedback) to the session log."""
        if self.event_log:
            self.event_log.append({"ts": time.time(), **event})

    def write_filtered(self, session_dir: Optional[Path | str] = None) -> Path:
        target = Path(session_dir) if session_dir else self.session_dir
        if target is None:
            raise RuntimeError("hub has no session dir (streams_dir not configured)")
        return _write_filtered(
            target,
            workspace=self.workspace_dir,
            goals_text=self.ctx.goals_text,
            drop_rule=describe_drop_rule(self.ctx.drop_threshold),
        )

"""Offline curator: wraps skills/focus-lab-curator/curate.py's PROMPT / slim /
extract_json_array and shells out to an agent CLI (default `claude --print`)
via asyncio subprocess. Not streaming; one verdict burst per batch. Anything
that goes wrong -> heuristic verdicts with stage="fallback"."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import AsyncIterator, Optional, Sequence

from src.models import Post
from src.stream.contracts import CuratorCaps, CuratorContext, Verdict

from . import apply_policy, post_to_dict, prefilter_verdict
from .claude_api import coerce_verdict
from .heuristic import HeuristicCurator

DEFAULT_SKILL_PATH = Path(__file__).resolve().parents[3] / "skills" / "focus-lab-curator" / "curate.py"


def load_curate_module(path: Path = DEFAULT_SKILL_PATH) -> ModuleType:
    spec = importlib.util.spec_from_file_location("focus_lab_curate", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load curate.py from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SkillCliCurator:
    name = "skill_cli"

    def __init__(self, cli: str = "claude", model: Optional[str] = None, *,
                 command: Optional[Sequence[str]] = None, timeout_s: float = 300.0,
                 max_batch: int = 20, skill_path: Path = DEFAULT_SKILL_PATH,
                 fallback: Optional[HeuristicCurator] = None):
        """`command` overrides argv entirely (prompt is sent on stdin)."""
        self.curate_mod = load_curate_module(skill_path)
        self.cli = cli
        self.model = model or (self.curate_mod.DEFAULT_CLAUDE_MODEL if cli == "claude" else None)
        self.command = list(command) if command else None
        self.timeout_s = timeout_s
        self.caps = CuratorCaps(max_batch=max_batch, streaming=False, latency_class="offline")
        self.fallback = fallback or HeuristicCurator()
        self.last_meta: dict = {}

    def _argv(self, prompt: str) -> tuple[list[str], Optional[str]]:
        if self.command:
            return self.command, prompt
        if self.cli == "claude":
            return ["claude", "--print", "--model", self.model], prompt
        if self.cli == "codex":
            return ["codex", "-q", *(["--model", self.model] if self.model else []), prompt], None
        if self.cli == "cursor-agent":
            return ["cursor-agent", "--print", "--output-format", "text",
                    *(["--model", self.model] if self.model else []), prompt], None
        raise ValueError(f"unsupported cli {self.cli!r}")

    async def _run(self, prompt: str) -> str:
        argv, stdin = self._argv(prompt)
        if shutil.which(argv[0]) is None and not Path(argv[0]).exists():
            raise FileNotFoundError(f"{argv[0]} not found in PATH")
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode() if stdin is not None else None), self.timeout_s)
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"{argv[0]} exit {proc.returncode}: {err.decode(errors='replace').strip()[:300]}")
        return out.decode(errors="replace")

    async def _batch(self, items: list[dict], goals: str, ctx: CuratorContext, fb) -> list[Verdict]:
        batch_id = uuid.uuid4().hex[:12]
        t0 = time.monotonic()
        m = self.curate_mod
        prompt = m.PROMPT.format(goals=goals.strip(),
                                 batch=json.dumps([m.slim(p) for p in items], indent=2, ensure_ascii=False))
        pending = {str(i.get("id", "")) for i in items}
        out: list[Verdict] = []
        error = None
        try:
            for obj in m.extract_json_array(await self._run(prompt)):
                if isinstance(obj, dict):
                    obj = {**obj, "reason": obj.get("reason") or obj.get("filter_reason", "")}
                    obj.setdefault("flags", [])
                    v = coerce_verdict(obj, pending)
                    if v:
                        pending.discard(v.id)
                        out.append(v)
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
        for item in items:
            if str(item.get("id", "")) in pending:
                v = fb(item)
                v.stage = "fallback"
                out.append(v)
        self.last_meta = {"batch_id": batch_id, "n": len(items), "n_fallback": len(pending),
                          "total_ms": int((time.monotonic() - t0) * 1000), "error": error,
                          "backend": self.name, "model": self.model, "goals_hash": ctx.goals_hash}
        return [apply_policy(v, ctx) for v in out]

    async def curate(self, posts: list[Post], ctx: CuratorContext) -> AsyncIterator[Verdict]:
        todo = []
        for p in posts:
            pre = prefilter_verdict(p)
            if pre:
                yield apply_policy(pre, ctx)
            else:
                todo.append(post_to_dict(p))
        n = self.caps.max_batch
        for i in range(0, len(todo), n):
            for v in await self._batch(todo[i:i + n], ctx.goals_text, ctx,
                                       lambda it: self.fallback.score_post(it, ctx)):
                yield v

    async def curate_replies(self, parent: Post, replies: list[dict], ctx: CuratorContext) -> AsyncIterator[Verdict]:
        pd = post_to_dict(parent)
        goals = (ctx.goals_text + "\n\nNOTE: the items below are REPLIES to this parent post "
                 f"by @{pd.get('author_handle', '')}: {(pd.get('text') or '')[:500]!r}. "
                 "Score how worth showing each reply is under it; insults/trolling/spam are drain.")
        items = [{"id": r.get("id"), "platform": pd.get("platform"), "text": r.get("text"),
                  "author_handle": r.get("author_handle"), "author_name": r.get("author_name")}
                 for r in replies]
        by_id = {str(r.get("id", "")): r for r in replies}
        n = self.caps.max_batch
        for i in range(0, len(items), n):
            for v in await self._batch(items[i:i + n], goals, ctx,
                                       lambda it: self.fallback.score_reply(by_id.get(str(it.get("id")), it), pd, ctx)):
                yield v

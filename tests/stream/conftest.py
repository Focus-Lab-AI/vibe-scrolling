"""Shared fixtures for streaming tests: fake feed site + curator doubles."""

from __future__ import annotations

import asyncio
import hashlib
from typing import AsyncIterator, Optional

import pytest
import pytest_asyncio

from src.models import Post
from src.stream.contracts import CuratorCaps, CuratorContext, Verdict
from tests.stream.fake_site import FakeSite


@pytest_asyncio.fixture
async def fake_site() -> AsyncIterator[FakeSite]:
    site = FakeSite()
    await site.start()
    try:
        yield site
    finally:
        await site.stop()


@pytest.fixture
def curator_ctx() -> CuratorContext:
    goals = "## What I'm working toward\n- learn machine learning\n## What brings me joy\n- cats\n"
    return CuratorContext(goals_text=goals, goals_hash=hashlib.sha256(goals.encode()).hexdigest())


class KeywordCurator:
    """Tiny deterministic curator double. Records batches; optional delay/fail."""

    name = "keyword-double"

    def __init__(self, max_batch: int = 8, delay: float = 0.0, fail: bool = False):
        self.caps = CuratorCaps(max_batch=max_batch)
        self.delay = delay
        self.fail = fail
        self.batches: list[list[str]] = []
        self.inflight = 0
        self.max_inflight_seen = 0

    def _verdict(self, pid: str, text: str) -> Verdict:
        t = text.lower()
        if any(w in t for w in ("idiot", "outrage", "furious", "disgrace", "moron")):
            return Verdict(id=pid, score=8, category="drain", flags=["rage_bait"], reason="rage")
        if any(w in t for w in ("like and follow", "tag 3 friends", "comment yes")):
            return Verdict(id=pid, score=40, category="neutral", flags=["engagement_bait"], reason="bait")
        if any(w in t for w in ("learn", "guide", "tutorial", "lessons")):
            return Verdict(id=pid, score=88, category="goal", reason="goal")
        if any(w in t for w in ("cat", "watercolor", "frog")):
            return Verdict(id=pid, score=70, category="joy", reason="joy")
        return Verdict(id=pid, score=45, category="neutral", reason="meh")

    async def curate(self, posts: list[Post], ctx: CuratorContext) -> AsyncIterator[Verdict]:
        self.batches.append([p.id for p in posts])
        self.inflight += 1
        self.max_inflight_seen = max(self.max_inflight_seen, self.inflight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("boom")
            for p in posts:
                yield self._verdict(p.id, p.text)
        finally:
            self.inflight -= 1

    async def curate_replies(
        self, parent: Post, replies: list[dict], ctx: CuratorContext
    ) -> AsyncIterator[Verdict]:
        for r in replies:
            yield self._verdict(str(r["id"]), r.get("text", ""))


@pytest.fixture
def keyword_curator() -> KeywordCurator:
    return KeywordCurator()


def real_or_double_curator(max_batch: Optional[int] = None):
    """HeuristicCurator if the curator package is available, else the double."""
    try:
        from src.stream.curator.heuristic import HeuristicCurator

        return HeuristicCurator(**({"max_batch": max_batch} if max_batch else {}))
    except Exception:  # noqa: BLE001
        return KeywordCurator(max_batch=max_batch or 8)

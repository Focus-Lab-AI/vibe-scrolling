"""Shared contracts for the streaming host.

Every module under src/stream/, app/api/stream.py and the phone client codes
against this file. Change it deliberately and update docs/streaming-plan.md.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import AsyncIterator, Literal, Optional, Protocol, runtime_checkable

from playwright.async_api import BrowserContext, Page, Response

from src.models import Post

Category = Literal["goal", "joy", "adjacent", "neutral", "drain"]
Action = Literal["keep", "boost", "drop"]
ReplyAction = Literal["show", "collapse", "hide"]
Stage = Literal["prefilter", "model", "fallback"]


# --------------------------------------------------------------------------
# Curation
# --------------------------------------------------------------------------

@dataclass
class Verdict:
    """Curator output for one post or reply. `action` is decided by policy
    code (see curator.policy), never by the model directly."""

    id: str
    score: int  # 0-100
    category: Category
    reason: str = ""
    confidence: float = 1.0
    flags: list[str] = field(default_factory=list)  # ad, rage_bait, engagement_bait, low_info
    action: Action = "keep"
    stage: Stage = "model"


@dataclass
class CuratorContext:
    goals_text: str
    goals_hash: str
    drop_threshold: Optional[int] = 19  # None = keep everything except drain


@dataclass
class CuratorCaps:
    max_batch: int = 8
    streaming: bool = True
    latency_class: Literal["realtime", "relaxed", "offline"] = "realtime"


@runtime_checkable
class Curator(Protocol):
    name: str
    caps: CuratorCaps

    def curate(self, posts: list[Post], ctx: CuratorContext) -> AsyncIterator[Verdict]:
        """Yield one Verdict per input post, as soon as each is known.
        Order may differ from input. Must not raise for bad model output —
        yield fallback verdicts (stage='fallback') instead."""
        ...

    def curate_replies(
        self, parent: Post, replies: list[dict], ctx: CuratorContext
    ) -> AsyncIterator[Verdict]:
        """Same as curate() but for replies to `parent`. Reply dicts carry at
        least id/text/author_handle. Verdict.action is mapped to ReplyAction
        by the hub: keep/boost→show, drop→hide; flags incl. rage_bait→collapse."""
        ...


# --------------------------------------------------------------------------
# Platforms
# --------------------------------------------------------------------------

@runtime_checkable
class PlatformAdapter(Protocol):
    """Streaming wrapper around a platform's existing interceptor/replies code."""

    platform: str
    home_url: str

    def matches(self, response: Response) -> bool:
        """Cheap URL check — is this response a feed payload?"""
        ...

    def parse_body(self, url: str, body: dict) -> list[Post]:
        """Pure: parse ONE response body into posts (ads included, is_ad set).
        Must be safe to call per response; no accumulation across calls."""
        ...

    async def extract_from_page(self, page: Page) -> list[Post]:
        """Posts embedded in the initial HTML (IG/YT/LI). Return [] if N/A."""
        ...

    async def is_logged_in(self, page: Page) -> bool:
        ...

    async def scroll_step(self, page: Page) -> None:
        """Advance the feed one human-sized step. Pacing is the producer's job."""
        ...

    async def fetch_comments(
        self, context: BrowserContext, post: Post, limit: int = 50
    ) -> list[dict]:
        """Open the post on the live context and return reply dicts
        (id, text, author_handle, author_name, created_at, likes). [] if unsupported."""
        ...


# --------------------------------------------------------------------------
# Events (host → phone). Serialized with to_wire().
# --------------------------------------------------------------------------

def _now() -> float:
    return time.time()


@dataclass
class CuratedPost:
    post: dict  # asdict(Post); media URLs rewritten to the host media proxy
    verdict: dict  # asdict(Verdict)
    curator: dict  # {backend, model, goals_hash, batch_id, latency_ms}
    platform: str
    seq: int = 0
    session_id: str = ""
    intercepted_at: float = field(default_factory=_now)
    type: str = "post.curated"
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=_now)
    v: int = 1


@dataclass
class CuratedReply:
    reply: dict
    action: ReplyAction
    flags: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class CuratedThread:
    request_id: str
    post_id: str
    replies: list[dict]  # asdict(CuratedReply)
    type: str = "thread.curated"
    ts: float = field(default_factory=_now)


@dataclass
class PlatformStatus:
    platform: str
    state: Literal["starting", "ready", "scrolling", "paused", "needs_login", "error", "stopped"]
    detail: str = ""
    raw_seen: int = 0
    kept: int = 0
    dropped: int = 0


def to_wire(event) -> dict:
    return asdict(event)

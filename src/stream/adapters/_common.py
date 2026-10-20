"""Shared helpers for the streaming platform adapters.

Private to src/stream/adapters/{x,threads,instagram,youtube,linkedin}.py.
Ideas for safe_close/bounded come from origin/feature/publish-feed
(src/platforms/reply_utils.py), re-implemented here so this branch does not
depend on that one.
"""

from __future__ import annotations

import asyncio
import random
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

# The legacy interceptors require a run_dir but only write to it from
# handle_response()/extract_from_page(), which the adapters never call.
PARSER_RUN_DIR = Path(tempfile.gettempdir()) / "vibe-stream-adapters"

CLOSE_TIMEOUT = 5.0
COMMENTS_TIMEOUT = 35.0  # goto (15s) + settle + margin


async def safe_close(page) -> None:
    """Close a page without ever hanging the caller."""
    try:
        await asyncio.wait_for(page.close(), timeout=CLOSE_TIMEOUT)
    except Exception:
        pass


async def bounded(coro: Awaitable[list], label: str, timeout: float = COMMENTS_TIMEOUT) -> list:
    """Await `coro` with a hard ceiling; return [] on timeout or error."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        print(f"[adapter] {label}: abandoned after {timeout}s")
        return []
    except Exception as e:  # noqa: BLE001 — a bad tab must never sink the caller
        print(f"[adapter] {label}: {type(e).__name__}: {e}")
        return []


async def human_wheel(
    page,
    *,
    base: int = 650,
    jitter: float = 0.35,
    hover_selectors: Iterable[str] = (),
) -> int:
    """One human-sized mouse-wheel step with jitter. Returns the delta used.

    If `hover_selectors` is given, the mouse is first moved over the first
    matching element so the wheel scrolls that container (LinkedIn).
    """
    dy = int(base * random.uniform(1 - jitter, 1 + jitter))
    x: Optional[float] = None
    y: Optional[float] = None
    for sel in hover_selectors:
        try:
            el = await page.query_selector(sel)
            box = await el.bounding_box() if el else None
        except Exception:
            box = None
        if box and box.get("width") and box.get("height"):
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            # Visible part of the container only.
            top = max(box["y"], 0)
            y = top + min(box["height"], 600) * random.uniform(0.3, 0.7)
            break
    if x is None:
        vp = getattr(page, "viewport_size", None) or {"width": 1280, "height": 800}
        x = vp["width"] * random.uniform(0.35, 0.65)
        y = vp["height"] * random.uniform(0.35, 0.65)
    try:
        await page.mouse.move(x, y, steps=random.randint(2, 5))
    except Exception:
        pass
    await page.mouse.wheel(0, dy)
    return dy


async def url_and_selectors_logged_in(
    page, logout_url_markers: Iterable[str], login_selectors: Iterable[str]
) -> bool:
    """Logged-out if the URL hits a login marker or a login form element exists."""
    url = getattr(page, "url", "") or ""
    if any(m in url for m in logout_url_markers):
        return False
    for sel in login_selectors:
        try:
            if await page.query_selector(sel):
                return False
        except Exception:
            continue
    return True


async def open_post_page(
    context,
    url: str,
    *,
    on_response: Optional[Callable[[Any], Awaitable[None]]] = None,
    settle_ms: int = 4000,
    ready: Optional[asyncio.Event] = None,
    goto_timeout_ms: int = 15000,
    work: Optional[Callable[[Any], Awaitable[list]]] = None,
    label: str = "",
    timeout: float = COMMENTS_TIMEOUT,
) -> list:
    """Open `url` in a new tab, optionally wait for `ready`, run `work(page)`.

    The page is always closed (bounded) and the whole thing is time-boxed.
    """
    page = await context.new_page()

    async def _run() -> list:
        if on_response is not None:
            page.on("response", on_response)
        await page.goto(url, wait_until="domcontentloaded", timeout=goto_timeout_ms)
        if ready is not None:
            try:
                await asyncio.wait_for(ready.wait(), timeout=settle_ms / 1000)
            except asyncio.TimeoutError:
                pass
        else:
            await page.wait_for_timeout(settle_ms)
        return await work(page) if work is not None else []

    try:
        return await bounded(_run(), label or url, timeout=timeout)
    finally:
        await safe_close(page)


def reply_dict(
    id: str,
    text: str,
    author_handle: str,
    author_name: str = "",
    created_at: str = "",
    likes: int = 0,
    **extra,
) -> dict:
    d = {
        "id": str(id),
        "text": text or "",
        "author_handle": author_handle or "",
        "author_name": author_name or author_handle or "",
        "created_at": str(created_at or ""),
        "likes": int(likes or 0),
    }
    d.update(extra)
    return d


def dedupe_by_id(posts: list) -> list:
    seen: set[str] = set()
    out = []
    for p in posts:
        if p is None or p.id in seen:
            continue
        seen.add(p.id)
        out.append(p)
    return out

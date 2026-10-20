"""Pull-driven feed producer: one per platform.

Attaches to the platform's feed page, turns intercepted feed responses into
`Post`s (deduped per producer), and scrolls only while the hub's
`DemandSignal` says the pipeline is below its high watermark.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Callable, Optional

from playwright.async_api import Page, Response

from src.models import Post
from src.stream.contracts import PlatformAdapter, PlatformStatus


class DemandSignal:
    """Low/high watermark hysteresis.

    `update(level)` with the number of posts already buffered downstream
    (phone + ready + in-pipeline). Scrolling stops once level >= high and
    resumes only when level <= low.
    """

    def __init__(self, low: int = 8, high: int = 20):
        if low >= high:
            raise ValueError("low watermark must be below high watermark")
        self.low = low
        self.high = high
        self.level = 0
        self.wanting = True
        self._changed = asyncio.Event()

    def update(self, level: int) -> bool:
        self.level = level
        before = self.wanting
        if self.wanting and level >= self.high:
            self.wanting = False
        elif not self.wanting and level <= self.low:
            self.wanting = True
        if before != self.wanting:
            self._changed.set()
        return self.wanting

    async def wait_changed(self, timeout: Optional[float] = None) -> None:
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass


@dataclass
class ProducerConfig:
    scroll_delay_min: float = 2.0
    scroll_delay_max: float = 5.0
    stale_after_steps: int = 12  # scroll steps with no new post → reload once
    session_cap_minutes: float = 30.0
    login_recheck_seconds: float = 3.0
    navigation_timeout_ms: int = 30000
    login_hint: str = "log in via live view or the desktop app"


class FeedProducer:
    def __init__(
        self,
        platform: str,
        adapter: PlatformAdapter,
        pool,
        out_queue: "asyncio.Queue[Post]",
        demand: DemandSignal,
        config: Optional[ProducerConfig] = None,
        on_status: Optional[Callable[[PlatformStatus], None]] = None,
    ):
        self.platform = platform
        self.adapter = adapter
        self.pool = pool
        self.out_queue = out_queue
        self.demand = demand
        self.config = config or ProducerConfig()
        self.on_status = on_status
        self.status = PlatformStatus(platform=platform, state="starting")
        self.seen: set[str] = set()
        self.page: Optional[Page] = None
        self.scroll_steps = 0
        self._paused = False
        self._resume = asyncio.Event()
        self._resume.set()
        self._login_kick = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._pending: set[asyncio.Task] = set()
        self._last_new_at_step = 0
        self._started_at = 0.0
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name=f"producer:{self.platform}")
        return self._task

    def recheck_login(self) -> None:
        """Re-run the login check now instead of at the next poll."""
        self._login_kick.set()

    def pause(self) -> None:
        self._paused = True
        self._resume.clear()

    def resume(self) -> None:
        self._paused = False
        self._resume.set()
        self.demand._changed.set()  # wake the loop

    async def stop(self) -> None:
        self._stopping = True
        self._resume.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        for t in list(self._pending):
            t.cancel()
        if self.page is not None:
            try:
                self.page.remove_listener("response", self._on_response)
            except Exception:
                pass
        self._set_state("stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def needs_login(self) -> bool:
        return self.running and self.status.state == "needs_login"

    def _set_state(self, state: str, detail: str = "") -> None:
        if self.status.state == state and self.status.detail == detail:
            return
        self.status.state = state  # type: ignore[assignment]
        self.status.detail = detail
        print(f"[stream] {self.platform}: {state}{' — ' + detail if detail else ''}")
        if self.on_status:
            try:
                self.on_status(self.status)
            except Exception as e:  # noqa: BLE001
                print(f"[stream] status callback failed: {e}")

    # -- response interception ---------------------------------------------
    def _on_response(self, response: Response) -> None:
        try:
            if not self.adapter.matches(response):
                return
        except Exception:
            return
        task = asyncio.create_task(self._handle_response(response))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _handle_response(self, response: Response) -> None:
        try:
            body = await response.json()
        except Exception as e:  # noqa: BLE001 — navigations kill bodies
            print(f"[stream] {self.platform}: unreadable feed body ({e})")
            return
        try:
            posts = self.adapter.parse_body(response.url, body)
        except Exception as e:  # noqa: BLE001
            print(f"[stream] {self.platform}: parse_body failed: {e}")
            return
        await self.emit(posts)

    async def emit(self, posts: list[Post]) -> int:
        new = 0
        for post in posts:
            key = f"{post.platform or self.platform}:{post.id}"
            if key in self.seen:
                continue
            self.seen.add(key)
            new += 1
            self.status.raw_seen += 1
            await self.out_queue.put(post)
        if new:
            self._last_new_at_step = self.scroll_steps
        return new

    # -- main loop -----------------------------------------------------------
    async def _run(self) -> None:
        cfg = self.config
        try:
            self._set_state("starting")
            self.page = await self.pool.get_page(self.platform, "feed")
            try:  # restart after stop/error must not double-attach
                self.page.remove_listener("response", self._on_response)
            except Exception:
                pass
            self.page.on("response", self._on_response)
            await self._navigate()
            if not await self._wait_logged_in():
                return
            try:
                await self.emit(await self.adapter.extract_from_page(self.page))
            except Exception as e:  # noqa: BLE001
                print(f"[stream] {self.platform}: extract_from_page failed: {e}")
            self._set_state("ready")
            self._started_at = time.monotonic()
            reloaded = False
            while not self._stopping:
                if self._paused:
                    self._set_state("paused", "manual")
                    await self._resume.wait()
                    continue
                if not self.demand.wanting:
                    self._set_state("paused", "buffer full")
                    await self.demand.wait_changed(timeout=5.0)
                    continue
                if time.monotonic() - self._started_at > cfg.session_cap_minutes * 60:
                    self._set_state("stopped", "session cap reached")
                    return
                if self.scroll_steps - self._last_new_at_step >= cfg.stale_after_steps:
                    if reloaded:
                        self._set_state("error", "feed stale: no new posts")
                        return
                    print(f"[stream] {self.platform}: feed looks stale, reloading")
                    reloaded = True
                    self._last_new_at_step = self.scroll_steps
                    await self._navigate()
                    continue
                self._set_state("scrolling")
                await self.adapter.scroll_step(self.page)
                self.scroll_steps += 1
                await asyncio.sleep(random.uniform(cfg.scroll_delay_min, cfg.scroll_delay_max))
                if self.scroll_steps - self._last_new_at_step < 2:
                    reloaded = False
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._set_state("error", str(e).splitlines()[0][:200])

    async def _navigate(self) -> None:
        assert self.page is not None
        await self.page.goto(
            self.adapter.home_url,
            wait_until="domcontentloaded",
            timeout=self.config.navigation_timeout_ms,
        )

    async def _wait_logged_in(self) -> bool:
        """Poll `is_logged_in` every `login_recheck_seconds` (or right away on
        `recheck_login()`) until the user has logged in in the browser window.
        After a manual login the page may be anywhere, so go back to the feed."""
        assert self.page is not None
        waited = False
        while not self._stopping:
            try:
                ok = await self.adapter.is_logged_in(self.page)
            except Exception as e:  # noqa: BLE001 — page mid-navigation etc.
                print(f"[stream] {self.platform}: login check failed: {e}")
                ok = False
            if ok:
                if waited:
                    await self._navigate()
                return True
            waited = True
            self._set_state("needs_login", self.config.login_hint)
            self._login_kick.clear()
            try:
                await asyncio.wait_for(self._login_kick.wait(), self.config.login_recheck_seconds)
            except asyncio.TimeoutError:
                pass
        return False

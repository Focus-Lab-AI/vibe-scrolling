"""Persistent browser contexts for the streaming host — one per platform.

Each platform gets its own `launch_persistent_context(user_data_dir=...)` so
cookies refresh naturally and the session survives restarts. Headed by
default (far less detectable); on a Linux host with no display we start a
private Xvfb server and point the browser at it. Tests pass `headless=True`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal, Optional

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    async_playwright,
)

from src.browser import IGNORE_DEFAULT_ARGS, LAUNCH_ARGS

STREAM_LAUNCH_ARGS = LAUNCH_ARGS + [
    # A hosted feed browser is never the foreground window; without these
    # Chrome throttles timers/rendering and infinite scroll stalls.
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
]

DEFAULT_VIEWPORT = {"width": 1280, "height": 900}
IMPORT_MARKER = ".vs_state_imported"

PageRole = Literal["feed", "detail"]


class Xvfb:
    """Minimal Xvfb launcher: picks a free display number, waits for the socket."""

    def __init__(self, start_display: int = 99, screen: str = "1920x1080x24"):
        self.start_display = start_display
        self.screen = screen
        self.display: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None

    @staticmethod
    def _is_free(n: int) -> bool:
        return not (
            Path(f"/tmp/.X{n}-lock").exists() or Path(f"/tmp/.X11-unix/X{n}").exists()
        )

    async def start(self) -> str:
        binary = shutil.which("Xvfb") or "/usr/bin/Xvfb"
        if not Path(binary).exists():
            raise RuntimeError("Xvfb not found; install it or run with headless=True")
        for n in range(self.start_display, self.start_display + 100):
            if not self._is_free(n):
                continue
            proc = subprocess.Popen(
                [binary, f":{n}", "-screen", "0", self.screen, "-nolisten", "tcp", "-ac"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            sock = Path(f"/tmp/.X11-unix/X{n}")
            for _ in range(50):
                if proc.poll() is not None:
                    break  # lost a race for this display; try next
                if sock.exists():
                    self._proc = proc
                    self.display = f":{n}"
                    print(f"[stream] Xvfb started on {self.display}")
                    return self.display
                await asyncio.sleep(0.1)
            if proc.poll() is None:
                proc.terminate()
        raise RuntimeError("Could not start Xvfb on any display")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


class BrowserPool:
    """Lazily launches one persistent context per platform."""

    def __init__(
        self,
        profiles_dir: Path | str,
        session_dir: Path | str | None = None,
        *,
        headless: bool = False,
        channel: Optional[str] = "chrome",
        viewport: Optional[dict] = None,
        session_files: Optional[dict[str, Path | str]] = None,
    ):
        self.profiles_dir = Path(profiles_dir)
        self.session_dir = Path(session_dir) if session_dir else None
        self.session_files = {k: Path(v) for k, v in (session_files or {}).items()}
        self.headless = headless
        self.channel = channel
        self.viewport = viewport or dict(DEFAULT_VIEWPORT)
        self._pw: Optional[Playwright] = None
        self._xvfb: Optional[Xvfb] = None
        self._env: Optional[dict] = None
        self._contexts: dict[str, BrowserContext] = {}
        self._pages: dict[tuple[str, str], Page] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._start_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    async def start(self) -> None:
        async with self._start_lock:
            if self._pw is not None:
                return
            if (
                not self.headless
                and sys.platform.startswith("linux")
                and not os.environ.get("DISPLAY")
                and not os.environ.get("WAYLAND_DISPLAY")
            ):
                self._xvfb = Xvfb()
                display = await self._xvfb.start()
                self._env = {**os.environ, "DISPLAY": display}
            self._pw = await async_playwright().start()

    @property
    def platforms(self) -> list[str]:
        return list(self._contexts)

    def _state_file(self, platform: str) -> Optional[Path]:
        if platform in self.session_files:
            return self.session_files[platform]
        if self.session_dir:
            return self.session_dir / f"{platform}_state.json"
        return None

    async def _launch(self, platform: str) -> BrowserContext:
        await self.start()
        assert self._pw is not None
        user_data_dir = self.profiles_dir / platform
        fresh = not user_data_dir.exists() or not any(user_data_dir.iterdir())
        user_data_dir.mkdir(parents=True, exist_ok=True)
        kwargs = dict(
            user_data_dir=str(user_data_dir),
            headless=self.headless,
            args=STREAM_LAUNCH_ARGS,
            ignore_default_args=IGNORE_DEFAULT_ARGS,
            viewport=self.viewport,
        )
        if self._env is not None:
            kwargs["env"] = self._env
        ctx: Optional[BrowserContext] = None
        if self.channel:
            try:
                ctx = await self._pw.chromium.launch_persistent_context(
                    channel=self.channel, **kwargs
                )
            except PlaywrightError as e:
                print(f"[stream] channel={self.channel} unavailable, using bundled chromium ({str(e).splitlines()[0]})")
        if ctx is None:
            ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
        await self._maybe_import_state(platform, ctx, user_data_dir, fresh)
        print(f"[stream] browser context ready for {platform} (profile {user_data_dir})")
        return ctx

    async def _maybe_import_state(
        self, platform: str, ctx: BrowserContext, user_data_dir: Path, fresh: bool
    ) -> None:
        """One-time import of a legacy storage_state JSON into a new profile."""
        marker = user_data_dir / IMPORT_MARKER
        if marker.exists():
            return
        state_file = self._state_file(platform)
        if not fresh or state_file is None or not state_file.exists():
            marker.write_text("skipped\n")
            return
        try:
            state = json.loads(state_file.read_text())
            cookies = state.get("cookies") or []
            if cookies:
                await ctx.add_cookies(cookies)
            marker.write_text(f"imported {len(cookies)} cookies from {state_file}\n")
            print(f"[stream] imported {len(cookies)} cookies for {platform} from {state_file}")
        except Exception as e:  # noqa: BLE001 — import is best-effort
            print(f"[stream] cookie import failed for {platform}: {e}")

    # ------------------------------------------------------------------
    async def get_context(self, platform: str) -> BrowserContext:
        lock = self._locks.setdefault(platform, asyncio.Lock())
        async with lock:
            ctx = self._contexts.get(platform)
            if ctx is None:
                ctx = await self._launch(platform)
                ctx.on("close", lambda _c, p=platform: self._forget(p))
                self._contexts[platform] = ctx
            return ctx

    def _forget(self, platform: str) -> None:
        self._contexts.pop(platform, None)
        for key in [k for k in self._pages if k[0] == platform]:
            self._pages.pop(key, None)

    async def get_page(self, platform: str, role: PageRole = "feed") -> Page:
        """Stable page per (platform, role); recreated if it was closed.
        The feed role reuses the tab a persistent context opens with."""
        ctx = await self.get_context(platform)
        page = self._pages.get((platform, role))
        if page is not None and not page.is_closed():
            return page
        taken = {id(p) for (pl, _), p in self._pages.items() if pl == platform}
        page = None
        if role == "feed":
            page = next(
                (p for p in ctx.pages if not p.is_closed() and id(p) not in taken), None
            )
        if page is None:
            page = await ctx.new_page()
        self._pages[(platform, role)] = page
        return page

    async def new_page(self, platform: str) -> Page:
        ctx = await self.get_context(platform)
        return await ctx.new_page()

    async def close_platform(self, platform: str) -> None:
        ctx = self._contexts.pop(platform, None)
        self._forget(platform)
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass

    async def close(self) -> None:
        for platform in list(self._contexts):
            await self.close_platform(platform)
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        if self._xvfb is not None:
            self._xvfb.stop()
            self._xvfb = None

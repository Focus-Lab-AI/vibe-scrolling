"""YouTube streaming adapter.

Reuses src/platforms/youtube/interceptor.py (_parse_grid_item ->
_parse_lockup_video / _parse_short).

Ported/added here:
- The ytInitialData -> richGridRenderer walk from extract_from_page as a pure
  `parse_initial_data` (legacy method mutates state and writes raw files).
- In-feed ads (`richItemRenderer.content.adSlotRenderer`) are emitted as
  is_ad=True posts; the legacy parser silently dropped them.
- `reloadContinuationItemsCommand` is handled alongside
  `appendContinuationItemsAction`.
- Shorts have no author in the feed payload; the legacy oEmbed enrichment
  (network) is intentionally not done here.
"""

from __future__ import annotations

import hashlib
import json

from playwright.async_api import BrowserContext, Page, Response

from src.models import Post
from src.platforms.youtube.interceptor import ResponseInterceptor

from ._common import PARSER_RUN_DIR, dedupe_by_id, human_wheel, url_and_selectors_logged_in

_AD_TEXT_KEYS = ("headline", "title", "description", "bodyText")


def _first_text(node, depth: int = 0) -> str:
    """Find a human-readable string under common YT text keys (ad renderers vary)."""
    if depth > 8:
        return ""
    if isinstance(node, dict):
        for k in _AD_TEXT_KEYS:
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):
                if isinstance(v.get("content"), str) and v["content"].strip():
                    return v["content"].strip()
                if isinstance(v.get("simpleText"), str):
                    return v["simpleText"].strip()
                runs = v.get("runs")
                if isinstance(runs, list) and runs:
                    return "".join(r.get("text", "") for r in runs if isinstance(r, dict)).strip()
        for v in node.values():
            t = _first_text(v, depth + 1)
            if t:
                return t
    elif isinstance(node, list):
        for v in node[:10]:
            t = _first_text(v, depth + 1)
            if t:
                return t
    return ""


def _parse_ad_slot(ad: dict) -> Post:
    slot_id = (ad.get("adSlotMetadata") or {}).get("slotId") or ""
    if not slot_id:
        slot_id = hashlib.sha1(
            json.dumps(ad, sort_keys=True).encode()
        ).hexdigest()[:16]
    return Post(
        id=f"ad:{slot_id}",
        platform="youtube",
        text=_first_text(ad),
        author_handle="",
        author_name="",
        created_at="",
        is_ad=True,
        platform_data={"type": "ad"},
    )


class YouTubeAdapter:
    platform = "youtube"
    home_url = "https://www.youtube.com/"

    LOGOUT_URL_MARKERS = ("accounts.google.com",)
    # Logged-out YouTube still renders a home feed, but with a "Sign in" button.
    LOGIN_SELECTORS = (
        'ytd-masthead a[href^="https://accounts.google.com/ServiceLogin"]',
        'ytd-masthead ytd-button-renderer a[aria-label="Sign in"]',
    )

    def __init__(self) -> None:
        self._parser = ResponseInterceptor(run_dir=PARSER_RUN_DIR)

    def matches(self, response: Response) -> bool:
        if "youtubei/v1/browse" not in (response.url or ""):
            return False
        status = getattr(response, "status", 200)
        return status == 200

    def _parse_items(self, items) -> list[Post]:
        posts: list[Post] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            posts.extend(self._parser._parse_grid_item(item))
            ad = ((item.get("richItemRenderer") or {}).get("content") or {}).get("adSlotRenderer")
            if ad:
                posts.append(_parse_ad_slot(ad))
        return dedupe_by_id(posts)

    def parse_body(self, url: str, body: dict) -> list[Post]:
        if not isinstance(body, dict):
            return []
        items: list = []
        for key in ("onResponseReceivedActions", "onResponseReceivedCommands"):
            for action in body.get(key) or []:
                for sub in ("appendContinuationItemsAction", "reloadContinuationItemsCommand"):
                    items.extend((action.get(sub) or {}).get("continuationItems") or [])
        return self._parse_items(items)

    def parse_initial_data(self, yt_data: dict) -> list[Post]:
        """Pure: ytInitialData -> posts (home richGrid)."""
        if not isinstance(yt_data, dict):
            return []
        try:
            tabs = (
                yt_data.get("contents", {})
                .get("twoColumnBrowseResultsRenderer", {})
                .get("tabs", [])
            )
            contents = (
                (tabs[0] if tabs else {})
                .get("tabRenderer", {})
                .get("content", {})
                .get("richGridRenderer", {})
                .get("contents", [])
            )
        except AttributeError:
            return []
        return self._parse_items(contents)

    async def extract_from_page(self, page: Page) -> list[Post]:
        try:
            yt_data = await page.evaluate("window.ytInitialData")
        except Exception as e:
            print(f"[adapter:youtube] ytInitialData eval failed: {e}")
            return []
        return self.parse_initial_data(yt_data or {})

    async def is_logged_in(self, page: Page) -> bool:
        return await url_and_selectors_logged_in(
            page, self.LOGOUT_URL_MARKERS, self.LOGIN_SELECTORS
        )

    async def scroll_step(self, page: Page) -> None:
        await human_wheel(page, base=750)

    async def fetch_comments(
        self, context: BrowserContext, post: Post, limit: int = 50
    ) -> list[dict]:
        return []

"""X (Twitter) streaming adapter.

Reuses src/platforms/x/interceptor.py (_extract_entries/_parse_entry/
_parse_tweet_result) and src/platforms/x/replies.py (_parse_replies_from_detail).

Ported/added here (legacy code untouched):
- Home timeline conversation modules (`TimelineTimelineModule`, entries with
  `content.items[]`) are parsed too; the batch collector skipped them.
- Reply text prefers `note_tweet` (long-form) over the truncated
  `legacy.full_text`, which the legacy reply parser always used.
"""

from __future__ import annotations

import asyncio
import re

from playwright.async_api import BrowserContext, Page, Response

from src.models import Post
from src.platforms.x.interceptor import ResponseInterceptor
from src.platforms.x.replies import TWEET_DETAIL_PATTERN, _parse_replies_from_detail

from ._common import (
    PARSER_RUN_DIR,
    dedupe_by_id,
    human_wheel,
    open_post_page,
    reply_dict,
    url_and_selectors_logged_in,
)

_STATUS_ID_RE = re.compile(r"/status/(\d+)")


def _note_tweet_texts(body) -> dict[str, str]:
    """Map tweet rest_id -> note_tweet text for every tweet result in `body`."""
    out: dict[str, str] = {}
    stack = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            rid = node.get("rest_id")
            if rid:
                text = (
                    (node.get("note_tweet") or {})
                    .get("note_tweet_results", {})
                    .get("result", {})
                    .get("text")
                )
                if text:
                    out[str(rid)] = text
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out


def parse_tweet_detail(body: dict, parent_id: str, limit: int = 50) -> list[dict]:
    """Pure: TweetDetail response body -> reply dicts (contract shape)."""
    replies = _parse_replies_from_detail(body, parent_id)
    long_text = _note_tweet_texts(body)
    out: list[dict] = []
    seen: set[str] = set()
    for r in replies:
        if r.id in seen:
            continue
        seen.add(r.id)
        out.append(
            reply_dict(
                r.id,
                long_text.get(r.id) or r.text,
                r.author_handle,
                r.author_name,
                r.created_at,
                r.likes,
                reposts=r.retweets,
                replies=r.replies,
                url=f"https://x.com/{r.author_handle}/status/{r.id}" if r.author_handle else "",
            )
        )
        if len(out) >= limit:
            break
    return out


class XAdapter:
    platform = "x"
    home_url = "https://x.com/home"

    GRAPHQL_PATTERN = ResponseInterceptor.GRAPHQL_PATTERN  # /i/api/graphql/.*/Home
    LOGOUT_URL_MARKERS = ("/login", "/i/flow/login", "/i/flow/signup")
    LOGIN_SELECTORS = (
        'input[autocomplete="username"]',
        '[data-testid="loginButton"]',
        '[data-testid="LoginForm_Login_Button"]',
    )
    # Max wait for the TweetDetail response after navigation.
    DETAIL_WAIT_MS = 10000

    def __init__(self) -> None:
        self._parser = ResponseInterceptor(run_dir=PARSER_RUN_DIR)

    def matches(self, response: Response) -> bool:
        return bool(self.GRAPHQL_PATTERN.search(response.url or ""))

    def parse_body(self, url: str, body: dict) -> list[Post]:
        if not isinstance(body, dict):
            return []
        posts: list[Post] = []
        for entry in self._parser._extract_entries(body):
            content = entry.get("content") or {}
            if content.get("itemContent"):
                posts.append(self._parser._parse_entry(entry))
                continue
            # Conversation module: content.items[].item.itemContent
            for item in content.get("items") or []:
                ic = (item.get("item") or {}).get("itemContent") or {}
                if ic.get("itemType") != "TimelineTweet":
                    continue
                result = (ic.get("tweet_results") or {}).get("result") or {}
                if result:
                    posts.append(
                        self._parser._parse_tweet_result(
                            result, is_ad="promotedMetadata" in ic
                        )
                    )
        return dedupe_by_id(posts)

    async def extract_from_page(self, page: Page) -> list[Post]:
        return []

    async def is_logged_in(self, page: Page) -> bool:
        return await url_and_selectors_logged_in(
            page, self.LOGOUT_URL_MARKERS, self.LOGIN_SELECTORS
        )

    async def scroll_step(self, page: Page) -> None:
        await human_wheel(page)

    async def fetch_comments(
        self, context: BrowserContext, post: Post, limit: int = 50
    ) -> list[dict]:
        url = post.url or (
            f"https://x.com/{post.author_handle}/status/{post.id}" if post.author_handle else ""
        )
        if not url:
            return []
        m = _STATUS_ID_RE.search(url)
        focal_id = m.group(1) if m else post.id  # retweets: url points at the original

        captured: dict = {}
        ready = asyncio.Event()

        async def on_response(response) -> None:
            if captured or not TWEET_DETAIL_PATTERN.search(response.url):
                return
            try:
                captured["body"] = await response.json()
                ready.set()
            except Exception:
                pass

        async def work(page) -> list[dict]:
            # Small settle so late-arriving detail responses can land.
            if not captured:
                await page.wait_for_timeout(1000)
            body = captured.get("body")
            return parse_tweet_detail(body, focal_id, limit) if body else []

        return await open_post_page(
            context,
            url,
            on_response=on_response,
            ready=ready,
            settle_ms=self.DETAIL_WAIT_MS,
            work=work,
            label=f"x comments {post.id}",
        )

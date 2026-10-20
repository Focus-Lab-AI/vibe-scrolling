"""Threads streaming adapter.

Reuses src/platforms/threads/interceptor.py (_parse_edge) and
src/platforms/threads/replies.py (_extract_replies_from_dom).

Ported/added here:
- matches() accepts threads.com and threads.net (legacy pattern: .com only).
- Extra ad signals `ad_id` / `dr_ad_type` on the post (same Meta media schema
  as Instagram) on top of the legacy `is_paid_partnership`. Unverified against
  live Threads ad payloads.
"""

from __future__ import annotations

import re

from playwright.async_api import BrowserContext, Page, Response

from src.models import Post
from src.platforms.threads.interceptor import ResponseInterceptor
from src.platforms.threads.replies import _extract_replies_from_dom

from ._common import (
    PARSER_RUN_DIR,
    dedupe_by_id,
    human_wheel,
    open_post_page,
    reply_dict,
    url_and_selectors_logged_in,
)


def _feed_data(body: dict) -> dict | None:
    data = body.get("data") or {}
    if not isinstance(data, dict):
        return None
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    return data.get("feedData") or inner.get("feedData")


class ThreadsAdapter:
    platform = "threads"
    home_url = "https://www.threads.com/"

    GRAPHQL_PATTERN = re.compile(r"threads\.(com|net)/graphql/query")
    LOGOUT_URL_MARKERS = ("/login",)
    LOGIN_SELECTORS = (
        'input[autocomplete="username"]',
        'a[href^="/login"]',
    )

    def __init__(self) -> None:
        self._parser = ResponseInterceptor(run_dir=PARSER_RUN_DIR)

    def matches(self, response: Response) -> bool:
        return bool(self.GRAPHQL_PATTERN.search(response.url or ""))

    def parse_body(self, url: str, body: dict) -> list[Post]:
        if not isinstance(body, dict):
            return []
        feed = _feed_data(body)
        if not feed:
            return []  # not a feed query (same filter as the legacy interceptor)
        posts: list[Post] = []
        for edge in feed.get("edges") or []:
            post = self._parser._parse_edge(edge)
            if post is None:
                continue
            items = (
                (edge.get("node") or {}).get("text_post_app_thread") or {}
            ).get("thread_items") or [{}]
            raw = items[0].get("post") or {}
            if raw.get("ad_id") or raw.get("dr_ad_type"):
                post.is_ad = True
            posts.append(post)
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
        if not post.url:
            return []

        async def work(page) -> list[dict]:
            replies = await _extract_replies_from_dom(page, post.author_handle, limit)
            # DOM gives no reply ids; username is unique per thread (legacy dedupes on it).
            return [
                reply_dict(r.id, r.text, r.author_handle, r.author_name, r.created_at, r.likes)
                for r in replies
            ]

        return await open_post_page(
            context,
            post.url,
            settle_ms=6000,
            work=work,
            label=f"threads comments {post.id}",
        )

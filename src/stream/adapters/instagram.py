"""Instagram streaming adapter.

Reuses src/platforms/instagram/interceptor.py (_parse_feed_edge,
_find_feed_posts) and src/platforms/instagram/replies.py
(_extract_comments_from_html, _find_comments).

Ported/added here:
- The HTML <script type="application/json"> scan from
  ResponseInterceptor.extract_from_page as a pure `parse_html` (legacy method
  mutates posts_by_id and writes raw files).
- fetch_comments also listens for graphql comment-connection responses on the
  post page, in addition to the legacy HTML scan.
"""

from __future__ import annotations

import json
import re

from playwright.async_api import BrowserContext, Page, Response

from src.models import Post
from src.platforms.instagram.interceptor import FEED_CONNECTION_KEY, ResponseInterceptor
from src.platforms.instagram.replies import (
    _extract_comments_from_html,
    _find_comments,
)

from ._common import (
    PARSER_RUN_DIR,
    dedupe_by_id,
    human_wheel,
    open_post_page,
    reply_dict,
    url_and_selectors_logged_in,
)

_JSON_SCRIPT_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.DOTALL
)


class InstagramAdapter:
    platform = "instagram"
    home_url = "https://www.instagram.com/"

    GRAPHQL_PATTERN = ResponseInterceptor.GRAPHQL_PATTERN  # instagram\.com/graphql/query
    LOGOUT_URL_MARKERS = ("/accounts/login", "/challenge/")
    LOGIN_SELECTORS = ('form#loginForm', 'input[name="username"][type="text"]')

    def __init__(self) -> None:
        self._parser = ResponseInterceptor(run_dir=PARSER_RUN_DIR)

    def matches(self, response: Response) -> bool:
        return bool(self.GRAPHQL_PATTERN.search(response.url or ""))

    def parse_body(self, url: str, body: dict) -> list[Post]:
        if not isinstance(body, dict):
            return []
        data = body.get("data") or {}
        conn = data.get(FEED_CONNECTION_KEY) if isinstance(data, dict) else None
        if not isinstance(conn, dict):
            return []  # some other graphql query
        return dedupe_by_id(
            [self._parser._parse_feed_edge(e) for e in conn.get("edges") or []]
        )

    def parse_html(self, html: str) -> list[Post]:
        """Pure: feed posts embedded in the page HTML's JSON script tags."""
        posts: list[Post] = []
        for script in _JSON_SCRIPT_RE.findall(html or ""):
            if FEED_CONNECTION_KEY not in script:
                continue  # cheap skip; most script tags are unrelated
            try:
                posts.extend(self._parser._find_feed_posts(json.loads(script)))
            except (json.JSONDecodeError, TypeError):
                continue
        return dedupe_by_id(posts)

    async def extract_from_page(self, page: Page) -> list[Post]:
        try:
            html = await page.content()
        except Exception as e:
            print(f"[adapter:instagram] page.content failed: {e}")
            return []
        return self.parse_html(html)

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
        from_api: list = []

        async def on_response(response) -> None:
            if not self.GRAPHQL_PATTERN.search(response.url):
                return
            try:
                body = await response.json()
            except Exception:
                return
            _find_comments(body, from_api, limit)  # looks for COMMENT_CONNECTION_KEY

        async def work(page) -> list[dict]:
            html = await page.content()
            return parse_comments(html, from_api, limit)

        return await open_post_page(
            context,
            post.url,
            on_response=on_response,
            settle_ms=6000,
            work=work,
            label=f"instagram comments {post.id}",
        )


def parse_comments(html: str, extra_replies: list | None = None, limit: int = 50) -> list[dict]:
    """Pure: merge comments from post-page HTML and any API-captured Reply objects."""
    replies = _extract_comments_from_html(html or "", limit) + list(extra_replies or [])
    out: list[dict] = []
    seen: set[str] = set()
    for r in replies:
        if not r.id or r.id in seen:
            continue
        seen.add(r.id)
        out.append(reply_dict(r.id, r.text, r.author_handle, r.author_name, r.created_at, r.likes))
        if len(out) >= limit:
            break
    return out

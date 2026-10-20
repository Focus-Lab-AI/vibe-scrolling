"""LinkedIn streaming adapter.

Reuses src/platforms/linkedin/interceptor.py (_build_post_from_update and its
media/text helpers, EXTRACT_JS for the DOM fallback).

Ported here: the `included` -> entityUrn index loop from _ingest_voyager
(legacy version accumulates into posts_by_id) and the DOM item -> Post mapping
from extract_from_page (legacy version mutates state).
"""

from __future__ import annotations

from playwright.async_api import BrowserContext, Page, Response

from src.models import Post
from src.platforms.linkedin.interceptor import EXTRACT_JS, ResponseInterceptor

from ._common import PARSER_RUN_DIR, dedupe_by_id, human_wheel, url_and_selectors_logged_in

UPDATE_TYPE = "com.linkedin.voyager.dash.feed.Update"


def dom_items_to_posts(items) -> list[Post]:
    """Pure: EXTRACT_JS output -> posts (DOM fallback)."""
    posts = []
    for item in items or []:
        urn = (item or {}).get("id") or ""
        if not urn:
            continue
        posts.append(
            Post(
                id=urn,
                platform="linkedin",
                text=item.get("text", ""),
                author_handle=item.get("author_name", ""),
                author_name=item.get("author_name", ""),
                created_at="",
                url=item.get("url", ""),
                platform_data={"activity_id": item.get("activity_id", ""), "source": "dom_fallback"},
            )
        )
    return dedupe_by_id(posts)


class LinkedInAdapter:
    platform = "linkedin"
    home_url = "https://www.linkedin.com/feed/"

    VOYAGER_PATTERN = ResponseInterceptor.VOYAGER_PATTERN
    LOGOUT_URL_MARKERS = ("/login", "/checkpoint", "/uas/login", "/authwall", "/signup")
    LOGIN_SELECTORS = ("form.login__form", "input#username", 'input[name="session_key"]')
    # The wheel must land on the feed, not the left/right rails.
    FEED_SELECTORS = (
        "main .scaffold-finite-scroll__content",
        "main[role='main']",
        "main",
    )

    def __init__(self) -> None:
        self._parser = ResponseInterceptor(run_dir=PARSER_RUN_DIR)

    def matches(self, response: Response) -> bool:
        if not self.VOYAGER_PATTERN.search(response.url or ""):
            return False
        return getattr(response, "status", 200) == 200

    def parse_body(self, url: str, body: dict) -> list[Post]:
        if not isinstance(body, dict):
            return []
        included = body.get("included")
        if not isinstance(included, list):
            return []
        store = {
            ent["entityUrn"]: ent
            for ent in included
            if isinstance(ent, dict) and ent.get("entityUrn")
        }
        return dedupe_by_id(
            [
                self._parser._build_post_from_update(ent, store)
                for ent in included
                if isinstance(ent, dict) and ent.get("$type") == UPDATE_TYPE
            ]
        )

    async def extract_from_page(self, page: Page) -> list[Post]:
        try:
            items = await page.evaluate(EXTRACT_JS)
        except Exception as e:
            print(f"[adapter:linkedin] DOM eval failed: {e}")
            return []
        return dom_items_to_posts(items)

    async def is_logged_in(self, page: Page) -> bool:
        return await url_and_selectors_logged_in(
            page, self.LOGOUT_URL_MARKERS, self.LOGIN_SELECTORS
        )

    async def scroll_step(self, page: Page) -> None:
        await human_wheel(page, base=600, hover_selectors=self.FEED_SELECTORS)

    async def fetch_comments(
        self, context: BrowserContext, post: Post, limit: int = 50
    ) -> list[dict]:
        return []

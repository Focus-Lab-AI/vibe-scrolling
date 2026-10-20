"""Adapter for the local fake feed site (tests/stream/fake_site)."""

from __future__ import annotations

import random
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, Response

from src.models import Post


class FakeAdapter:
    platform = "fake"

    def __init__(self, base_url: str, platform: str = "fake"):
        self.base_url = base_url.rstrip("/")
        self.home_url = self.base_url + "/"
        self.platform = platform

    # -- feed ------------------------------------------------------------
    def matches(self, response: Response) -> bool:
        return urlparse(response.url).path == "/api/feed"

    def parse_body(self, url: str, body: dict) -> list[Post]:
        posts: list[Post] = []
        for raw in body.get("posts") or []:
            try:
                posts.append(self._to_post(raw))
            except (KeyError, TypeError):
                continue
        return posts

    def _to_post(self, raw: dict) -> Post:
        author = raw.get("author") or {}
        return Post(
            id=str(raw["id"]),
            platform=self.platform,
            text=raw.get("text") or "",
            author_handle=author.get("handle", ""),
            author_name=author.get("name", ""),
            created_at=raw.get("created_at", ""),
            url=f"{self.base_url}/post/{raw['id']}",
            likes=int(raw.get("likes") or 0),
            reposts=int(raw.get("reposts") or 0),
            replies=int(raw.get("replies") or 0),
            media_urls=list(raw.get("media") or []),
            is_ad=bool(raw.get("promoted")),
            platform_data={"kind": raw.get("kind")},
        )

    async def extract_from_page(self, page: Page) -> list[Post]:
        return []

    async def is_logged_in(self, page: Page) -> bool:
        return urlparse(page.url).path != "/login"

    async def scroll_step(self, page: Page) -> None:
        vp = page.viewport_size or {"width": 1280, "height": 900}
        await page.mouse.move(
            vp["width"] / 2 + random.uniform(-80, 80), vp["height"] / 2 + random.uniform(-80, 80)
        )
        await page.mouse.wheel(0, random.randint(700, 1100))

    # -- comments ----------------------------------------------------------
    async def fetch_comments(
        self, context: BrowserContext, post: Post, limit: int = 50
    ) -> list[dict]:
        page = await context.new_page()
        try:
            async with page.expect_response(
                lambda r: urlparse(r.url).path == f"/api/post/{post.id}/comments",
                timeout=15000,
            ) as info:
                await page.goto(f"{self.base_url}/post/{post.id}")
            body = await (await info.value).json()
        finally:
            await page.close()
        out = []
        for c in (body.get("comments") or [])[:limit]:
            author = c.get("author") or {}
            out.append(
                {
                    "id": str(c.get("id")),
                    "text": c.get("text") or "",
                    "author_handle": author.get("handle", ""),
                    "author_name": author.get("name", ""),
                    "created_at": c.get("created_at", ""),
                    "likes": int(c.get("likes") or 0),
                }
            )
        return out

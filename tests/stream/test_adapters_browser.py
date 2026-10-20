"""Browser-facing adapter methods, exercised with in-memory fake pages."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.models import Post
from src.stream.adapters.instagram import InstagramAdapter
from src.stream.adapters.linkedin import LinkedInAdapter
from src.stream.adapters.threads import ThreadsAdapter
from src.stream.adapters.x import XAdapter
from src.stream.adapters.youtube import YouTubeAdapter

FIX = Path(__file__).parent / "fixtures"
ALL = [XAdapter, ThreadsAdapter, InstagramAdapter, YouTubeAdapter, LinkedInAdapter]


class FakeMouse:
    def __init__(self):
        self.wheels: list[tuple[float, float]] = []
        self.moves: list[tuple[float, float]] = []

    async def move(self, x, y, steps=1):
        self.moves.append((x, y))

    async def wheel(self, dx, dy):
        self.wheels.append((dx, dy))


class FakeElement:
    def __init__(self, box):
        self._box = box

    async def bounding_box(self):
        return self._box


class FakePage:
    def __init__(self, url="https://example.com/", selectors=None, html="", evaluate_result=None,
                 responses=None, goto_hangs=False, close_hangs=False):
        self.url = url
        self.viewport_size = {"width": 1280, "height": 800}
        self.mouse = FakeMouse()
        self._selectors = selectors or {}
        self._html = html
        self._evaluate_result = evaluate_result
        self._responses = responses or []
        self._handlers = []
        self._goto_hangs = goto_hangs
        self._close_hangs = close_hangs
        self.closed = False
        self.evaluated = []

    async def query_selector(self, sel):
        return self._selectors.get(sel)

    def on(self, event, handler):
        assert event == "response"
        self._handlers.append(handler)

    async def goto(self, url, **kw):
        if self._goto_hangs:
            await asyncio.sleep(3600)
        self.url = url
        for r in self._responses:
            for h in self._handlers:
                await h(r)

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)

    async def content(self):
        return self._html

    async def evaluate(self, expr, *args):
        self.evaluated.append((expr, args))
        return self._evaluate_result

    async def close(self):
        if self._close_hangs:
            await asyncio.sleep(3600)
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.opened = 0

    async def new_page(self):
        self.opened += 1
        return self.page


class FakeResponse:
    def __init__(self, url, body):
        self.url = url
        self.status = 200
        self._body = body

    async def json(self):
        return self._body


def post(**kw) -> Post:
    base = dict(id="1001", platform="x", text="t", author_handle="alice", author_name="A", created_at="")
    base.update(kw)
    return Post(**base)


# ------------------------------------------------------------------- scroll


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ALL)
async def test_scroll_step_is_one_jittered_wheel(cls):
    deltas = set()
    for _ in range(8):
        page = FakePage()
        await cls().scroll_step(page)
        assert len(page.mouse.wheels) == 1
        dx, dy = page.mouse.wheels[0]
        assert dx == 0 and 250 <= dy <= 1100
        deltas.add(dy)
        assert not page.evaluated, "must not scrollTo via JS"
    assert len(deltas) > 1, "wheel delta should be jittered"


@pytest.mark.asyncio
async def test_linkedin_scroll_hovers_feed_container():
    box = {"x": 400, "y": 60, "width": 560, "height": 3000}
    page = FakePage(selectors={"main": FakeElement(box)})
    await LinkedInAdapter().scroll_step(page)
    x, y = page.mouse.moves[-1]
    assert 400 <= x <= 960 and 60 <= y <= 660


# ---------------------------------------------------------------- logged in


LOGIN_CASES = [
    (XAdapter, "https://x.com/home", "https://x.com/i/flow/login", 'input[autocomplete="username"]'),
    (ThreadsAdapter, "https://www.threads.com/", "https://www.threads.com/login?next=/", 'input[autocomplete="username"]'),
    (InstagramAdapter, "https://www.instagram.com/", "https://www.instagram.com/accounts/login/", "form#loginForm"),
    (YouTubeAdapter, "https://www.youtube.com/", "https://accounts.google.com/v3/signin",
     'ytd-masthead a[href^="https://accounts.google.com/ServiceLogin"]'),
    (LinkedInAdapter, "https://www.linkedin.com/feed/", "https://www.linkedin.com/authwall?trk=x", "input#username"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("cls,home,login_url,login_sel", LOGIN_CASES)
async def test_is_logged_in(cls, home, login_url, login_sel):
    a = cls()
    assert await a.is_logged_in(FakePage(url=home)) is True
    assert await a.is_logged_in(FakePage(url=login_url)) is False
    assert await a.is_logged_in(FakePage(url=home, selectors={login_sel: object()})) is False


# ------------------------------------------------------------ page extract


@pytest.mark.asyncio
async def test_extract_from_page():
    assert await XAdapter().extract_from_page(FakePage()) == []
    assert await ThreadsAdapter().extract_from_page(FakePage()) == []

    html = (FIX / "instagram/home.html").read_text()
    ig = await InstagramAdapter().extract_from_page(FakePage(html=html))
    assert {p.id for p in ig} == {"4201", "4202"}

    yt_data = json.loads((FIX / "youtube/yt_initial_data.json").read_text())
    page = FakePage(evaluate_result=yt_data)
    yt = await YouTubeAdapter().extract_from_page(page)
    assert page.evaluated[0][0] == "window.ytInitialData"
    assert "vidInit0001" in {p.id for p in yt}
    assert await YouTubeAdapter().extract_from_page(FakePage(evaluate_result=None)) == []

    li_items = [{"id": "urn:li:activity:5", "text": "x", "author_name": "N", "url": "u", "activity_id": "5"}]
    li = await LinkedInAdapter().extract_from_page(FakePage(evaluate_result=li_items))
    assert [p.id for p in li] == ["urn:li:activity:5"]


# ----------------------------------------------------------------- comments


@pytest.mark.asyncio
async def test_x_fetch_comments_intercepts_tweet_detail_and_closes():
    body = json.loads((FIX / "x/tweet_detail.json").read_text())
    page = FakePage(responses=[
        FakeResponse("https://x.com/i/api/graphql/zz/HomeTimeline", {"nope": 1}),
        FakeResponse("https://x.com/i/api/graphql/zz/TweetDetail?variables=1", body),
    ])
    ctx = FakeContext(page)
    out = await XAdapter().fetch_comments(ctx, post(url="https://x.com/alice/status/1001"), limit=2)
    assert [r["id"] for r in out] == ["2001", "2002"]
    assert page.closed and page.url == "https://x.com/alice/status/1001"


@pytest.mark.asyncio
async def test_x_fetch_comments_builds_url_and_handles_no_detail():
    page = FakePage()
    a = XAdapter()
    a.DETAIL_WAIT_MS = 50
    out = await a.fetch_comments(FakeContext(page), post(url=""))
    assert out == [] and page.url == "https://x.com/alice/status/1001" and page.closed


@pytest.mark.asyncio
async def test_fetch_comments_timeout_still_closes(monkeypatch):
    import src.stream.adapters._common as common

    monkeypatch.setattr(common, "COMMENTS_TIMEOUT", 0.05)
    monkeypatch.setattr(common, "CLOSE_TIMEOUT", 0.05)
    page = FakePage(goto_hangs=True, close_hangs=True)
    # open_post_page binds the default at definition time, so pass timeout explicitly.
    out = await common.open_post_page(FakeContext(page), "https://x.com/a/status/1", timeout=0.05)
    assert out == []


@pytest.mark.asyncio
async def test_instagram_fetch_comments_html_plus_api():
    html = (FIX / "instagram/post_comments.html").read_text()
    api_body = {"data": {"xdt_api__v1__media__media_id__comments__connection": {"edges": [
        {"node": {"__typename": "XDTCommentDict", "pk": "5003", "text": "from api",
                  "user": {"username": "pia"}, "comment_like_count": 1}},
        {"node": {"__typename": "XDTCommentDict", "pk": "5001", "text": "dup",
                  "user": {"username": "nina"}}},
    ]}}}
    page = FakePage(html=html, responses=[FakeResponse("https://www.instagram.com/graphql/query", api_body)])
    p = post(id="4101", platform="instagram", url="https://www.instagram.com/p/IGa/")
    out = await InstagramAdapter().fetch_comments(FakeContext(page), p)
    assert [c["id"] for c in out] == ["5001", "5002", "5003"]
    assert page.closed


@pytest.mark.asyncio
async def test_threads_fetch_comments_uses_dom_heuristic():
    raw = [
        {"username": "zoe", "displayName": "Zoe Z", "text": "nice one"},
        {"username": "zoe", "displayName": "Zoe Z", "text": "again"},
        {"username": "hana", "displayName": "Hana", "text": "op reply"},
        {"username": "yan", "displayName": "Yan", "text": "agreed"},
    ]
    page = FakePage(evaluate_result=raw)
    p = post(id="3101", platform="threads", author_handle="hana",
             url="https://www.threads.net/@hana/post/AbC1")
    out = await ThreadsAdapter().fetch_comments(FakeContext(page), p, limit=5)
    assert [(c["id"], c["author_handle"], c["text"]) for c in out] == [
        ("zoe", "zoe", "nice one"),
        ("yan", "yan", "agreed"),
    ]
    assert page.evaluated[0][1] == ("hana",)
    assert page.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [YouTubeAdapter, LinkedInAdapter])
async def test_unsupported_fetch_comments(cls):
    ctx = FakeContext(FakePage())
    assert await cls().fetch_comments(ctx, post()) == []
    assert ctx.opened == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [ThreadsAdapter, InstagramAdapter])
async def test_fetch_comments_without_url(cls):
    ctx = FakeContext(FakePage())
    assert await cls().fetch_comments(ctx, post(url="")) == []
    assert ctx.opened == 0

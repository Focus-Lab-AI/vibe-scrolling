"""A tiny deterministic infinite-feed site (aiohttp).

Routes:
  GET /                        infinite feed page; JS fetches /api/feed?cursor=N
                               on load and whenever the user scrolls near bottom
  GET /api/feed?cursor=N       {"posts": [...], "next_cursor": N+1}
  GET /post/<id>               detail page; JS fetches /api/post/<id>/comments
  GET /api/post/<id>/comments  {"post_id": id, "comments": [...]}
  GET /login                   page the feed redirects to when logged_out=True

Post `i` (global index) is deterministic:
  i % 10 == 0  → ad (is_ad)
  i % 10 == 3  → rage-bait-ish text
  i % 10 == 5  → goal-ish text (learning / building)
  i % 10 == 7  → joy (cats, art)
  i % 20 == 19 → empty post (no text, no media)
  otherwise    → neutral chatter
Each page also repeats the last post of the previous page (dedupe fodder).
Texts carry a unique " [i]" suffix.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Optional

from aiohttp import web

PAGE_SIZE = 10

RAGE = [
    "You won't BELIEVE how stupid these idiots are. Outrage! Retweet if you agree!!!",
    "This is a DISGRACE and everyone involved should be ashamed. Absolutely furious.",
    "Hot take: anyone who disagrees with me is a moron. Fight me in the replies.",
]
GOAL = [
    "How I learned machine learning in 6 months: a step-by-step guide with resources.",
    "Thread: 10 lessons from shipping my first side project as a solo founder.",
    "A practical tutorial on writing async Python that is easy to test.",
]
JOY = [
    "My cat just discovered the cardboard box. 10/10 would watch again.",
    "Finished a watercolor of the harbor at sunset — first painting in years!",
    "Tiny frog on a leaf after the rain. Nature is delightful.",
]
NEUTRAL = [
    "Coffee number two. Tuesday is going fine, I guess.",
    "Anyone else think the new bus schedule is a bit confusing?",
    "Reading list for the weekend is getting long again.",
    "Weather today: cloudy with a chance of more clouds.",
]


def make_post(i: int) -> dict:
    kind = "neutral"
    text = NEUTRAL[i % len(NEUTRAL)]
    media: list[str] = []
    is_ad = False
    if i % 20 == 19:
        kind, text = "empty", ""
    elif i % 10 == 0:
        kind, is_ad = "ad", True
        text = "Sponsored: Buy the new SuperWidget today — 50% off!"
    elif i % 10 == 3:
        kind, text = "rage", RAGE[(i // 10) % len(RAGE)]
    elif i % 10 == 5:
        kind, text = "goal", GOAL[(i // 10) % len(GOAL)]
    elif i % 10 == 7:
        kind, text = "joy", JOY[(i // 10) % len(JOY)]
        media = [f"https://picsum.photos/seed/fake{i}/600/400"]
    if text:
        text = f"{text} [{i}]"  # unique, so text-hash dedupe only hits real repeats
    return {
        "id": f"p{i}",
        "kind": kind,
        "text": text,
        "author": {"handle": f"user{i % 13}", "name": f"User {i % 13}"},
        "created_at": f"2026-10-20T{(i // 60) % 24:02d}:{i % 60:02d}:00Z",
        "likes": (i * 37) % 500,
        "reposts": (i * 11) % 90,
        "replies": 8,
        "media": media,
        "promoted": is_ad,
    }


def make_feed_page(cursor: int, page_size: int = PAGE_SIZE) -> list[dict]:
    start = cursor * page_size
    posts = [make_post(i) for i in range(start, start + page_size)]
    if cursor > 0:
        posts.insert(0, make_post(start - 1))  # duplicate across pages
    return posts


def make_comments(post_id: str) -> list[dict]:
    texts = [
        ("Great point, thanks for sharing!", "neutral"),
        ("You are all idiots and this is a disgrace. Outrage!", "rage"),
        ("Like and follow for more! Comment YES if you agree!", "bait"),
        ("I tried this last week and it worked for me.", "neutral"),
        ("This is the dumbest thing I've read. Absolutely furious.", "rage"),
        ("Tag 3 friends who need to see this!!! Follow me", "bait"),
        ("Interesting — do you have a source for the second part?", "neutral"),
        ("Lovely, made my day.", "neutral"),
    ]
    return [
        {
            "id": f"{post_id}_c{j}",
            "text": t,
            "kind": kind,
            "author": {"handle": f"replier{j}", "name": f"Replier {j}"},
            "created_at": "2026-10-20T12:00:00Z",
            "likes": j * 3,
        }
        for j, (t, kind) in enumerate(texts)
    ]


FEED_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>FakeFeed</title>
<style>
 body { font-family: sans-serif; margin: 0; }
 .post { height: 260px; border-bottom: 1px solid #ccc; padding: 12px; box-sizing: border-box; }
 #status { padding: 12px; }
</style></head>
<body>
<div id="feed" data-testid="feed"></div>
<div id="status">loading…</div>
<script>
let cursor = 0, loading = false;
async function loadMore() {
  if (loading) return;
  loading = true;
  try {
    const r = await fetch('/api/feed?cursor=' + cursor);
    const data = await r.json();
    const feed = document.getElementById('feed');
    for (const p of data.posts) {
      const d = document.createElement('div');
      d.className = 'post';
      d.dataset.id = p.id;
      d.innerHTML = '<a href="/post/' + p.id + '">@' + p.author.handle + '</a><p></p>';
      d.querySelector('p').textContent = p.text;
      feed.appendChild(d);
    }
    cursor = data.next_cursor;
    document.getElementById('status').textContent = 'cursor ' + cursor;
  } finally { loading = false; }
}
function nearBottom() {
  return window.innerHeight + window.scrollY >= document.body.scrollHeight - 1200;
}
window.addEventListener('scroll', () => { if (nearBottom()) loadMore(); }, {passive: true});
loadMore();
</script></body></html>
"""

DETAIL_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Post</title></head>
<body><div id="post" data-id="__ID__"></div><ul id="comments"></ul>
<script>
fetch('/api/post/__ID__/comments').then(r => r.json()).then(data => {
  const ul = document.getElementById('comments');
  for (const c of data.comments) {
    const li = document.createElement('li'); li.textContent = c.text; ul.appendChild(li);
  }
});
</script></body></html>
"""

LOGIN_HTML = """<!doctype html><html><body><form id="login"><input name="u"></form></body></html>"""


@dataclass
class FakeSite:
    page_size: int = PAGE_SIZE
    latency: float = 0.0
    logged_out: bool = False
    feed_requests: list[int] = field(default_factory=list)
    comment_requests: list[str] = field(default_factory=list)
    url: str = ""
    _runner: Optional[web.AppRunner] = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/login", self._login)
        app.router.add_get("/api/feed", self._feed)
        app.router.add_get("/post/{post_id}", self._detail)
        app.router.add_get("/api/post/{post_id}/comments", self._comments)
        return app

    async def _index(self, request: web.Request) -> web.StreamResponse:
        if self.logged_out:
            raise web.HTTPFound("/login")
        return web.Response(text=FEED_HTML, content_type="text/html")

    async def _login(self, request: web.Request) -> web.Response:
        return web.Response(text=LOGIN_HTML, content_type="text/html")

    async def _feed(self, request: web.Request) -> web.Response:
        cursor = int(request.query.get("cursor", "0"))
        self.feed_requests.append(cursor)
        if self.latency:
            await asyncio.sleep(self.latency)
        body = {"posts": make_feed_page(cursor, self.page_size), "next_cursor": cursor + 1}
        return web.Response(text=json.dumps(body), content_type="application/json")

    async def _detail(self, request: web.Request) -> web.Response:
        pid = request.match_info["post_id"]
        return web.Response(text=DETAIL_HTML.replace("__ID__", pid), content_type="text/html")

    async def _comments(self, request: web.Request) -> web.Response:
        pid = request.match_info["post_id"]
        self.comment_requests.append(pid)
        body = {"post_id": pid, "comments": make_comments(pid)}
        return web.Response(text=json.dumps(body), content_type="application/json")

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        self._runner = web.AppRunner(self.app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        sock = site._server.sockets[0]  # type: ignore[union-attr]
        actual_port = sock.getsockname()[1]
        self.url = f"http://{host}:{actual_port}"
        return self.url

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


async def start_fake_site(host: str = "127.0.0.1", port: int = 0, **kw) -> FakeSite:
    site = FakeSite(**kw)
    await site.start(host, port)
    return site

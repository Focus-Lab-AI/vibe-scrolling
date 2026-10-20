#!/usr/bin/env python3
"""Mock streaming host for developing the phone PWA without browsers/logins.

Serves app/static (so /phone/ and /js/post-renderer.js work) and a fake
`/ws/stream` that speaks the wire protocol from docs/streaming-plan.md:

  client → host: hello, demand, open_comments, live.start/stop, live.input
  host → client: session, post.curated, thread.curated, status, live.meta,
                 binary live frames (0x01 + JPEG), error

Behaviour:
  * x / threads stream plausible curated posts (inline SVG data-URI media,
    no network) until the client's reported buffer_depth (+ posts sent since
    that report) reaches HIGH_WATERMARK (20); resumes on the next demand.
  * instagram reports needs_login; live.start streams a static fake login
    frame and records live.input events (app.state.live_inputs).
  * linkedin reports needs_login; live.start replies error not_implemented.
  * open_comments → thread.curated with show/collapse/hide replies after ~800ms.

Run:  .venv/bin/python scripts/mock_stream_server.py [--port 8765]
Open: http://<host>:8765/phone/?token=dev
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import itertools
import json
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "app" / "static"

HIGH_WATERMARK = 20
FEED_PLATFORMS = ["x", "threads"]
LOGIN_PLATFORMS = {"instagram": "frames", "linkedin": "not_implemented"}
FRAME_TAG_JPEG = b"\x01"

_post_ids = itertools.count(1)

# ---------------------------------------------------------------------------
# Fake content
# ---------------------------------------------------------------------------

AUTHORS = {
    "x": [
        ("Simon Willison", "simonw"), ("Julia Evans", "b0rk"), ("Andrej Karpathy", "karpathy"),
        ("Hillel Wayne", "hillelogram"), ("Dan Abramov", "dan_abramov"), ("Amelia Wattenberger", "Wattenberger"),
        ("Rachel Kroll", "rakyll_ish"), ("Bret Victor-ish", "worrydream_"),
    ],
    "threads": [
        ("Maya Chen", "maya.makes"), ("Leo Park", "leo.climbs"), ("Studio Ghibli Fans", "ghibli.daily"),
        ("Priya R", "priya.reads"), ("Night Sky Photos", "nightsky.pics"), ("Tom's Garden", "tomsgarden"),
    ],
}

TEXTS = [
    "Spent the weekend rewriting our job queue on top of Postgres SKIP LOCKED. 400 lines deleted, latency halved. Boring tech wins again.",
    "A thread on how I learned to read papers faster:\n\n1. Read the figures first\n2. Then the conclusion\n3. Only then the method\n\nMost papers are one figure plus 9 pages of justification.",
    "TIL you can pipe `git log -p` into `delta` and it becomes a genuinely pleasant way to review history. #git",
    "Sunrise from the ridge this morning. Worth the 4:30 alarm.",
    "New blog post: Building a tiny vector index in 200 lines of Python — no dependencies, surprisingly decent recall. https://example.com/tiny-index",
    "If your onboarding doc has more than 10 steps, your onboarding is the bug.",
    "Finally finished the sourdough experiment. 78% hydration, 18h cold proof. The crumb!!",
    "Hot take: most \"AI agents\" are a while loop and a good prompt, and that's fine. Ship the loop.",
    "Reminder that sleep is the best performance-enhancing drug and it's free.",
    "Watercolor practice, day 41. Still can't do skies but the trees are getting there.",
    "We moved our CI from 22 minutes to 6 by (a) caching the right thing and (b) deleting tests that tested nothing. Writeup soon.",
    "The best debugging tool is still explaining the problem out loud to someone who doesn't care. @b0rk has a zine on exactly this.",
    "Climbing gym PR today: first clean V5. Took 3 months of hangboard and approximately 900 falls.",
    "Small joys: the library emailed to say the book I reserved in March is ready.",
    "Rust's borrow checker is a strict but fair teacher. You don't appreciate it until you go back to a codebase without it.",
    "Our team's rule for meetings: no agenda, no meeting. Cut our calendar by a third.",
    "Night sky over the desert — Milky Way core visible to the naked eye. 25s exposure, f/2.8, ISO 3200.",
    "Reading list for the month: Designing Data-Intensive Applications (again), The Overstory, and a pile of Ted Chiang.",
    "Long post incoming because a few people asked how we structure our design reviews. " + (
        "We start with the problem statement, not the solution. Everyone reads the doc silently for ten minutes at the "
        "start of the meeting, then we go round the table. Disagreement is expected and written down. The author owns "
        "the final call, but has to respond to every written concern before the doc is marked approved. It's slower "
        "up front and dramatically faster overall."),
    "Tiny CSS trick: `text-wrap: balance` on headings makes everything look 20% more designed. #css",
]

QUOTED = [
    {"author_name": "Grace Hopper (archive)", "author_handle": "gracehopper_quotes",
     "text": "The most dangerous phrase in the language is: we've always done it this way."},
    {"author_name": "Paul Graham", "author_handle": "paulg",
     "text": "The way to get startup ideas is not to try to think of startup ideas."},
]

CATEGORIES = [("goal", 45), ("joy", 25), ("adjacent", 20), ("neutral", 10)]
REASONS = {
    "goal": ["Deep dive on systems design — matches your 'get better at backend' goal.",
             "Practical engineering lesson with concrete numbers.",
             "Learning technique you said you wanted to try."],
    "joy": ["Outdoors / photography — you asked for more of this.",
            "Small wholesome moment from someone you follow.",
            "Creative hobby progress, low-stakes and uplifting."],
    "adjacent": ["Related to your interests, not directly a goal.",
                 "Interesting opinion, light on substance."],
    "neutral": ["Fine, but nothing special.", "General update from an account you follow."],
}

PALETTES = [("#ff7e5f", "#feb47b"), ("#6a11cb", "#2575fc"), ("#11998e", "#38ef7d"),
            ("#fc5c7d", "#6a82fb"), ("#1e3c72", "#2a5298"), ("#f7971e", "#ffd200"),
            ("#0f2027", "#2c5364"), ("#ee9ca7", "#ffdde1")]

REPLY_TEXTS_SHOW = [
    "This is super helpful, saving it for later.",
    "We hit the exact same thing last year — the fix was boring and it worked.",
    "Great writeup. Did you measure p99 too, or just median?",
    "Love this. The photo of the second step is especially clear.",
    "Counterpoint: it depends a lot on team size, but I mostly agree.",
    "Bookmarked. Would read a longer version of this.",
]
REPLY_COLLAPSE = [
    ("Anyone who believes this is a clown. Unfollow.", "rage bait"),
    ("Like + RT if you agree!!! 🔥🔥🔥 Follow for more", "engagement bait"),
    ("Actually this is wrong and you should feel bad.", "hostile tone"),
]
REPLY_HIDE = [
    "Make $5000/week from home, DM me 💸",
    "Check my profile for crypto signals 🚀🚀",
    "first",
]


def svg_data_uri(title: str, w: int, h: int, seed: int) -> str:
    c1, c2 = PALETTES[seed % len(PALETTES)]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient></defs>'
        f'<rect width="{w}" height="{h}" fill="url(#g)"/>'
        f'<circle cx="{w * 0.78:.0f}" cy="{h * 0.28:.0f}" r="{min(w, h) * 0.12:.0f}" fill="#fff" opacity=".25"/>'
        f'<path d="M0 {h * 0.78:.0f} Q{w * 0.3:.0f} {h * 0.55:.0f} {w * 0.55:.0f} {h * 0.75:.0f} T{w} {h * 0.7:.0f} V{h} H0Z" fill="#000" opacity=".18"/>'
        f'<text x="50%" y="52%" text-anchor="middle" font-family="Helvetica,Arial,sans-serif" '
        f'font-size="{min(w, h) * 0.07:.0f}" font-weight="700" fill="#fff" opacity=".92">{title}</text>'
        f"</svg>"
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def make_post(rng: random.Random) -> dict:
    n = next(_post_ids)
    platform = rng.choice(FEED_PLATFORMS)
    name, handle = rng.choice(AUTHORS[platform])
    text = rng.choice(TEXTS)
    post_id = f"mock-{platform}-{n}"
    created = datetime.now(timezone.utc) - timedelta(minutes=rng.randint(1, 60 * 30))

    media: list[str] = []
    roll = rng.random()
    if roll < 0.30:
        w, h = rng.choice([(1200, 800), (1080, 1350), (1000, 1000)])
        media = [svg_data_uri(f"photo #{n}", w, h, n)]
    elif roll < 0.48:
        k = rng.randint(2, 4)
        media = [svg_data_uri(f"{i + 1} / {k}", 1080, 1350, n + i) for i in range(k)]

    post = {
        "id": post_id,
        "platform": platform,
        "text": text,
        "author_handle": handle,
        "author_name": name,
        "created_at": created.isoformat(),
        "url": f"https://example.com/{platform}/{handle}/{n}",
        "likes": int(rng.paretovariate(1.2) * 20),
        "reposts": int(rng.paretovariate(1.4) * 4),
        "replies": rng.randint(3, 120),
        "quotes": 0,
        "media_urls": media,
        "video_urls": [],
        "local_media_paths": [],
        "is_repost": rng.random() < 0.08,
        "original_author": rng.choice(AUTHORS[platform])[1],
        "quoted_post": rng.choice(QUOTED) if rng.random() < 0.08 else None,
        "is_ad": False,
        "top_replies": [],
        "platform_data": {},
    }
    if not post["is_repost"]:
        post["original_author"] = None

    category = rng.choices([c for c, _ in CATEGORIES], [w for _, w in CATEGORIES])[0]
    score = {"goal": rng.randint(70, 98), "joy": rng.randint(60, 90),
             "adjacent": rng.randint(40, 70), "neutral": rng.randint(20, 45)}[category]
    action = "boost" if (score >= 85 and rng.random() < 0.6) else "keep"
    verdict = {
        "id": post_id, "score": score, "category": category,
        "reason": rng.choice(REASONS[category]), "confidence": round(rng.uniform(0.6, 0.99), 2),
        "flags": [], "action": action, "stage": "model",
    }
    return {
        "post": post,
        "verdict": verdict,
        "curator": {"backend": "mock", "model": "mock-haiku", "goals_hash": "mock",
                    "batch_id": uuid.uuid4().hex[:8], "latency_ms": rng.randint(300, 1400)},
        "platform": platform,
        "seq": n,
        "session_id": "",
        "intercepted_at": time.time(),
        "type": "post.curated",
        "event_id": uuid.uuid4().hex,
        "ts": time.time(),
        "v": 1,
    }


def make_thread(request_id: str, post_id: str, rng: random.Random) -> dict:
    replies = []

    def reply(text: str, i: int) -> dict:
        name, handle = rng.choice(AUTHORS["x"] + AUTHORS["threads"])
        return {
            "id": f"{post_id}-r{i}", "text": text, "author_handle": handle, "author_name": name,
            "created_at": (datetime.now(timezone.utc) - timedelta(minutes=rng.randint(1, 600))).isoformat(),
            "likes": rng.randint(0, 300),
        }

    i = 0
    shows = rng.sample(REPLY_TEXTS_SHOW, 5)
    collapses = rng.sample(REPLY_COLLAPSE, 2)
    hides = rng.sample(REPLY_HIDE, 2)
    order = (["show"] * 2) + ["collapse"] + (["show"] * 2) + ["hide", "collapse", "show", "hide"]
    for action in order:
        i += 1
        if action == "show":
            replies.append({"reply": reply(shows.pop(), i), "action": "show", "flags": [], "reason": ""})
        elif action == "collapse":
            text, why = collapses.pop()
            replies.append({"reply": reply(text, i), "action": "collapse",
                            "flags": [why.replace(" ", "_")], "reason": why})
        else:
            replies.append({"reply": reply(hides.pop(), i), "action": "hide",
                            "flags": ["spam"], "reason": "spam"})
    return {"request_id": request_id, "post_id": post_id, "replies": replies,
            "type": "thread.curated", "ts": time.time()}


# ---------------------------------------------------------------------------
# Per-connection session
# ---------------------------------------------------------------------------

class MockSession:
    def __init__(self, ws: WebSocket, app: FastAPI):
        self.ws = ws
        self.app = app
        self.rng = random.Random()
        self.lock = asyncio.Lock()
        self.session_id = uuid.uuid4().hex[:12]
        self.platforms: list[str] = list(FEED_PLATFORMS)
        self.reported_depth = 0
        self.sent_since_demand = 0
        self.demand_event = asyncio.Event()
        self.stats = {p: {"raw_seen": 0, "kept": 0, "dropped": 0} for p in FEED_PLATFORMS}
        self.tasks: list[asyncio.Task] = []
        self.live_task: asyncio.Task | None = None
        self.started = False

    async def send_json(self, msg: dict) -> None:
        async with self.lock:
            await self.ws.send_text(json.dumps(msg))

    async def send_bytes(self, data: bytes) -> None:
        async with self.lock:
            await self.ws.send_bytes(data)

    @property
    def estimated_depth(self) -> int:
        return self.reported_depth + self.sent_since_demand

    def platform_states(self) -> list[dict]:
        paused = self.estimated_depth >= HIGH_WATERMARK
        out = []
        for p in FEED_PLATFORMS:
            out.append({"platform": p, "state": "paused" if paused else "scrolling",
                        "detail": "buffer full" if paused else "", **self.stats[p]})
        for p in LOGIN_PLATFORMS:
            out.append({"platform": p, "state": "needs_login", "detail": "session expired",
                        "raw_seen": 0, "kept": 0, "dropped": 0})
        return out

    async def on_hello(self, msg: dict) -> None:
        self.reported_depth = 0
        self.sent_since_demand = 0
        await self.send_json({
            "type": "session", "session_id": self.session_id,
            "platforms": [{"platform": p["platform"],
                           "status": "needs_login" if p["state"] == "needs_login" else "ready"}
                          for p in self.platform_states()],
        })
        if not self.started:
            self.started = True
            self.tasks.append(asyncio.create_task(self.produce()))
            self.tasks.append(asyncio.create_task(self.status_loop()))
        self.demand_event.set()

    async def on_demand(self, msg: dict) -> None:
        self.reported_depth = int(msg.get("buffer_depth") or 0)
        self.sent_since_demand = 0
        self.app.state.demands.append(msg)
        self.demand_event.set()

    async def produce(self) -> None:
        while True:
            if self.estimated_depth >= HIGH_WATERMARK:
                self.demand_event.clear()
                try:
                    await asyncio.wait_for(self.demand_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            # Some raw posts get dropped by the "curator" and never reach the phone.
            event = make_post(self.rng)
            st = self.stats[event["platform"]]
            dropped = self.rng.random() < 0.3
            st["raw_seen"] += 1 + int(dropped)
            st["dropped"] += int(dropped)
            st["kept"] += 1
            event["session_id"] = self.session_id
            self.app.state.known_posts.add(event["post"]["id"])
            await self.send_json(event)
            self.sent_since_demand += 1
            await asyncio.sleep(self.rng.uniform(0.05, 0.25))

    async def status_loop(self) -> None:
        while True:
            await self.send_json({
                "type": "status", "platforms": self.platform_states(),
                "buffer_depth": self.estimated_depth,
                "curator_latency_ms": self.rng.randint(400, 1200), "ts": time.time(),
            })
            await asyncio.sleep(1.5)

    async def on_open_comments(self, msg: dict) -> None:
        request_id = msg.get("request_id") or ""
        post_id = str(msg.get("post_id") or "")

        async def reply() -> None:
            await asyncio.sleep(0.8)
            if post_id not in self.app.state.known_posts:
                await self.send_json({"type": "error", "request_id": request_id, "code": "not_found",
                                      "message": f"Unknown post {post_id!r}"})
                return
            await self.send_json(make_thread(request_id, post_id, self.rng))

        self.tasks.append(asyncio.create_task(reply()))

    async def on_live_start(self, msg: dict) -> None:
        platform = msg.get("platform")
        if LOGIN_PLATFORMS.get(platform) != "frames":
            await self.send_json({"type": "error", "request_id": msg.get("request_id"),
                                  "code": "not_implemented",
                                  "message": f"live view not_implemented for {platform}"})
            return
        await self.stop_live()
        await self.send_json({"type": "live.meta", "platform": platform,
                              "device_width": 270, "device_height": 480})
        frame = FRAME_TAG_JPEG + base64.b64decode(LIVE_FRAME_JPEG_B64)

        async def stream() -> None:
            for _ in range(600):  # ~5 min cap
                await self.send_bytes(frame)
                await asyncio.sleep(0.5)

        self.live_task = asyncio.create_task(stream())

    async def stop_live(self) -> None:
        if self.live_task:
            self.live_task.cancel()
            self.live_task = None

    async def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "hello":
            await self.on_hello(msg)
        elif kind == "demand":
            await self.on_demand(msg)
        elif kind == "open_comments":
            await self.on_open_comments(msg)
        elif kind == "live.start":
            await self.on_live_start(msg)
        elif kind == "live.stop":
            await self.stop_live()
        elif kind == "live.input":
            self.app.state.live_inputs.append(msg)
        elif kind == "feedback":
            pass
        else:
            await self.send_json({"type": "error", "request_id": msg.get("request_id"),
                                  "code": "bad_request", "message": f"unknown message type {kind!r}"})

    async def close(self) -> None:
        await self.stop_live()
        for t in self.tasks:
            t.cancel()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(title="vibe-scrolling mock stream host")
    app.state.known_posts = set()
    app.state.live_inputs = []
    app.state.demands = []
    app.state.messages = []

    @app.get("/")
    async def root():
        return RedirectResponse("/phone/")

    @app.get("/phone")
    async def phone_no_slash():
        return RedirectResponse("/phone/")

    @app.websocket("/ws/stream")
    async def ws_stream(ws: WebSocket):
        if not ws.query_params.get("token"):
            await ws.close(code=4401)
            return
        await ws.accept()
        session = MockSession(ws, app)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                app.state.messages.append(msg)
                await session.handle(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await session.close()

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()


def main() -> None:
    import socket

    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    try:
        lan = socket.gethostbyname(socket.gethostname())
    except OSError:
        lan = "127.0.0.1"
    print(f"Phone client: http://127.0.0.1:{args.port}/phone/?token=dev")
    print(f"         LAN: http://{lan}:{args.port}/phone/?token=dev")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


# Fake "login page" frame for live view (270x480 JPEG, rendered once offline).
LIVE_FRAME_JPEG_B64 = (
    "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"
    "w9p+lX2f/sBt9ZodNro1R1VfWRqsjbBbmsSXY+Xb9z5Hz/pRxnWcS6VQ4JTq56XSRujTNwlt3N4y393mXuPolFNen09dFMdt"
    "dcVGKy3hLkubNPxHonwXiWues1Wmbtljftm4qf34A4Xp1pOFcNs0ui4dulek5XyldKb9Czl4T7fMjuOFRXAuhFU5LDo0rukn"
    "6zTljveCV/RHgGocHZw6HzI7IqNk4pL7k16XzMt3Rng9+jp0lullKijdsh11ixueXl7uf9wPnPRDgd3HJ8QnHVWUOFTjui8b"
    "5SzhN+jlzNr/AA84pZoeJX8D1mYdZJuEZf6bFya/ul+XvOx0PRjg3D5zno9I6pWQcJNXTfJ9va+X39pX8i+ju7d/h73duevs"
    "z/3AcT/Eids+k8a55UI0QUM9mG3l9+e46niXRrQafgs1r+NcVehprWYO+O3C7Elt+7C+43nEuC8N4rTCrXaWFqrWIPLUo/c1"
    "zK2l6McL0zg3Xbeq3mEb7pTjF+6LePyA5njGg0/Cf4e2rhq1ap1dsLJfKMb0njtwuS+au8zfw0+T6fgev1dtkINXfzG39GMY"
    "ppvvZvekvGNBw/5Lo+JUxs0+ulKFjk+UIrHNrHPm0ctxWvojwrSXWcL6vVa6yO2iELXaoyfY/Ry7efPIGu6Pv/Hv4g/LHF9X"
    "1stRh+aMfo/ntNz/ABS1mNPodDF/SlK2S+5YX6ss/wAPOAX8Oou4hranVdfFQrhJYlGHa8rzZeO43Go6IcB1N07r9DKdk5OU"
    "pO+zm28v/UBl6J6JaDo1oaduJSrVk/vl87/fH9jjulHGdZxLpVDglOrnpdJG6NM3CW3c3jLf3eZe473h3DdJwvTujQ1Outy3"
    "NOcpc8JdrbfmNdxHonwXiWues1Wmbtljftm4qf34A4Xp1pOFcNs0ui4dulek5XyldKb9Czl4T7fMjuOFRXAuhNU5LDo0rukn"
    "6zTljveCV/RHgGocHZw6HzI7IqNk4pL7k16XzMt3Rng9+jp0lullKijdsh11ixueXl7uf9wOL/hlpXfxfV66eZdTXty/Wk+3"
    "uT7ytxD/AI5/ElUr51cdRGvH/TD6X6SO603RTgmlVq0+jcOtg4TxdZzi/wD5cuwhV0O6P03Rur0DVkXuT6+x8/xAcz/FLWZn"
    "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"
    "AAAAAAAAAAAAAAAAAAAA/9k="
)

if __name__ == "__main__":
    main()

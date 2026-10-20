"""End-to-end: fake site → headless chromium → hub → WS client."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
import pytest_asyncio
import uvicorn
import websockets
from fastapi import FastAPI

from app.api import stream as stream_api
from src.stream.adapters.fake import FakeAdapter
from src.stream.browser_pool import BrowserPool
from src.stream.hub import HubConfig, StreamHub
from src.stream.producer import ProducerConfig
from tests.stream.conftest import real_or_double_curator


async def _serve(app: FastAPI):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, port


@pytest_asyncio.fixture
async def host(fake_site, tmp_path, monkeypatch):
    monkeypatch.delenv("VS_STREAM_TOKEN", raising=False)
    monkeypatch.setattr(stream_api, "stream_token", lambda: None)
    pool = BrowserPool(tmp_path / "profiles", headless=True, channel=None)
    cfg = HubConfig(
        low_watermark=8, high_watermark=20, batch_window=0.3,
        producer=ProducerConfig(scroll_delay_min=0.05, scroll_delay_max=0.15, stale_after_steps=30),
    )
    hub = StreamHub(pool, {"fake": FakeAdapter(fake_site.url)}, real_or_double_curator(), config=cfg,
                    streams_dir=tmp_path / "streams", workspace_dir=tmp_path)
    stream_api.set_hub(hub)
    app = FastAPI()
    app.include_router(stream_api.router)
    server, task, port = await _serve(app)
    try:
        yield {"hub": hub, "site": fake_site, "port": port, "app": app}
    finally:
        server.should_exit = True
        await task
        stream_api.set_hub(None)
        await hub.close(close_pool=True)


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.posts: list[dict] = []
        self.other: list[dict] = []

    async def send(self, **msg):
        await self.ws.send(json.dumps(msg))

    async def recv_until(self, pred, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            (self.posts if msg["type"] == "post.curated" else self.other).append(msg)
            if pred(msg):
                return msg
        raise AssertionError(f"timed out; got {len(self.posts)} posts, other={[m['type'] for m in self.other]}")


async def _wait(cond, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.mark.asyncio
async def test_spine_e2e_stream(host):
    hub, site, port = host["hub"], host["site"], host["port"]
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/stream") as ws:
        c = Client(ws)
        await c.send(type="hello", platforms=["fake"], buffer_target=40)
        session = await c.recv_until(lambda m: m["type"] == "session", 10)
        assert session["session_id"] == hub.session_id

        # ≥10 curated posts quickly, no ads, nothing dropped delivered.
        await c.recv_until(lambda m: len(c.posts) >= 10, 30)
        for m in c.posts:
            assert m["post"]["is_ad"] is False
            assert m["post"]["platform_data"]["kind"] not in ("ad", "empty")
            assert m["verdict"]["action"] in ("keep", "boost")
        assert len({m["post"]["id"] for m in c.posts}) == len(c.posts)

        # Phone reports a full buffer → producer pauses, no more feed requests.
        await c.send(type="demand", buffer_depth=100, last_seen_post_id=c.posts[-1]["post"]["id"])
        prod = hub.producers["fake"]
        await _wait(lambda: prod.status.state == "paused", 10)
        await asyncio.sleep(0.5)  # let in-flight responses land
        n_req = len(site.feed_requests)
        await asyncio.sleep(1.5)
        assert len(site.feed_requests) <= n_req + 1
        assert prod.status.state == "paused"

        # Phone drained → producer resumes and new posts arrive.
        # (A real phone keeps reporting as it consumes; ready-buffer posts are
        # delivered first, so keep reporting an empty buffer until it scrolls.)
        before = len(c.posts)
        deadline = time.monotonic() + 30
        while len(site.feed_requests) <= n_req + 1 or len(c.posts) < before + 5:
            assert time.monotonic() < deadline, "producer did not resume"
            await c.send(type="demand", buffer_depth=0)
            try:
                await c.recv_until(lambda m: False, 0.3)
            except AssertionError:
                pass

        # Comments on the real (fake) browser → curated thread.
        pid = c.posts[0]["post"]["id"]
        await c.send(type="open_comments", request_id="req-1", post_id=pid)
        thread = await c.recv_until(lambda m: m["type"] == "thread.curated", 30)
        assert thread["request_id"] == "req-1" and thread["post_id"] == pid
        actions = [r["action"] for r in thread["replies"]]
        assert len(actions) == 8 and "show" in actions
        assert any(a in ("hide", "collapse") for a in actions)
        assert pid in site.comment_requests

        # Unknown post + live view placeholders → errors.
        await c.send(type="open_comments", request_id="req-2", post_id="does-not-exist")
        err = await c.recv_until(lambda m: m["type"] == "error" and m.get("request_id") == "req-2", 10)
        assert "unknown post" in err["message"]
        await c.send(type="live.start", platform="fake", request_id="req-3")
        err = await c.recv_until(lambda m: m["type"] == "error" and m.get("request_id") == "req-3", 10)
        assert err["message"] == "not_implemented"

    # Event log has drops (ads) with bodies omitted; filtered file reconciles.
    path = hub.write_filtered()
    data = json.loads(path.read_text())
    meta = data["filter_metadata"]
    assert meta["source_posts"] == meta["kept_posts"] + meta["dropped_count"]
    assert any(d["id"].startswith("p") and d["category"] for d in meta["dropped"])
    dropped_ids = {d["id"] for d in meta["dropped"]}
    assert "p0" in dropped_ids  # p0 is an ad
    assert hub.counters["dup_id"] == 0  # producer dedupes the repeated post first


@pytest.mark.asyncio
async def test_spine_ws_token_and_status(host, monkeypatch):
    port = host["port"]
    monkeypatch.setattr(stream_api, "stream_token", lambda: "s3cret")
    with pytest.raises(Exception):
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/stream?token=wrong") as ws:
            await ws.recv()
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/stream?token=s3cret") as ws:
        await ws.send(json.dumps({"type": "demand", "buffer_depth": 0}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert msg["type"] == "error" and "hello" in msg["message"]
        await ws.send("not json")
        msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert msg["type"] == "error"

    import aiohttp

    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{port}/api/stream/status") as r:
            assert r.status == 401
        async with s.get(f"http://127.0.0.1:{port}/api/stream/status?token=s3cret") as r:
            body = await r.json()
            assert r.status == 200 and body["configured_platforms"] == ["fake"]


def test_spine_create_app_registers_stream_routes(monkeypatch):
    from starlette.testclient import TestClient

    from app.server import create_app

    monkeypatch.setattr(stream_api, "stream_token", lambda: None)
    stream_api.set_hub(None)
    client = TestClient(create_app())
    r = client.get("/api/stream/status")
    assert r.status_code == 200 and r.json()["running"] is False
    assert any(getattr(m, "path", None) == "/phone" for m in client.app.routes)

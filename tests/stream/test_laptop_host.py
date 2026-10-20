"""Laptop host: login in the visible browser window, hello restarts, CLI config."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
import pytest_asyncio
import websockets
from fastapi import FastAPI

from app.api import stream as stream_api
from src.stream.adapters.fake import FakeAdapter
from src.stream.browser_pool import BrowserPool
from src.stream.hub import HubConfig, StreamHub
from src.stream.producer import ProducerConfig
from tests.stream.conftest import real_or_double_curator
from tests.stream.test_spine_e2e import Client, _serve

HINT = "Log in in the browser window on your laptop."
SCROLLING = ("ready", "scrolling", "paused")


async def wait_for(cond, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


def _hub(site, tmp_path, recheck: float) -> StreamHub:
    pool = BrowserPool(tmp_path / "profiles", headless=True, channel=None)
    cfg = HubConfig(
        batch_window=0.2, host_mode="laptop", login_hint=HINT,
        producer=ProducerConfig(scroll_delay_min=0.05, scroll_delay_max=0.1,
                                login_recheck_seconds=recheck, login_hint=HINT),
    )
    return StreamHub(pool, {"fake": FakeAdapter(site.url)}, real_or_double_curator(), config=cfg,
                     streams_dir=tmp_path / "streams", workspace_dir=tmp_path)


async def _log_in_on_laptop(site, prod) -> None:
    """What the user does in the visible window: sign in, land on the feed."""
    site.logged_out = False
    await prod.page.goto(site.url + "/", wait_until="domcontentloaded")


@pytest_asyncio.fixture
async def logged_out_site(fake_site):
    fake_site.logged_out = True
    return fake_site


@pytest.mark.asyncio
async def test_needs_login_then_ready_after_laptop_login(logged_out_site, tmp_path):
    site = logged_out_site
    hub = _hub(site, tmp_path, recheck=0.3)
    try:
        await hub.start(["fake"])
        prod = hub.producers["fake"]
        await wait_for(lambda: prod.status.state == "needs_login", 15)
        assert prod.status.detail == HINT
        session = hub.session_message()
        assert session["platforms"][0]["status"] == "needs_login"
        assert session["host"] == {"mode": "laptop", "login_hint": HINT}
        assert prod.status.raw_seen == 0

        await _log_in_on_laptop(site, prod)
        t0 = time.monotonic()
        await wait_for(lambda: prod.status.state in SCROLLING and prod.status.raw_seen > 0, 15)
        assert time.monotonic() - t0 < 10
        assert prod.running
    finally:
        await hub.close(close_pool=True)


@pytest_asyncio.fixture
async def laptop_host(logged_out_site, tmp_path, monkeypatch):
    monkeypatch.setattr(stream_api, "stream_token", lambda: None)
    # Poll interval far longer than the test: only hello's recheck can unblock it.
    hub = _hub(logged_out_site, tmp_path, recheck=60)
    stream_api.set_hub(hub)
    app = FastAPI()
    app.include_router(stream_api.router)
    server, task, port = await _serve(app)
    try:
        yield {"hub": hub, "site": logged_out_site, "port": port}
    finally:
        server.should_exit = True
        await task
        stream_api.set_hub(None)
        await hub.close(close_pool=True)


@pytest.mark.asyncio
async def test_hello_rechecks_needs_login_and_restarts_stopped(laptop_host):
    hub, site, port = laptop_host["hub"], laptop_host["site"], laptop_host["port"]
    url = f"ws://127.0.0.1:{port}/ws/stream"
    async with websockets.connect(url) as ws:
        c = Client(ws)
        await c.send(type="hello", platforms=["fake"], buffer_target=40)
        session = await c.recv_until(lambda m: m["type"] == "session", 10)
        assert session["host"]["mode"] == "laptop"
        prod = hub.producers["fake"]
        await wait_for(lambda: prod.status.state == "needs_login", 15)

        # Logged in on the laptop; the next 60s poll is far away — hello rechecks now.
        await _log_in_on_laptop(site, prod)
        await asyncio.sleep(0.5)
        assert prod.status.state == "needs_login"
        await c.send(type="hello", platforms=["fake"], buffer_target=40)
        await wait_for(lambda: prod.status.state in SCROLLING, 10)
        await c.recv_until(lambda m: len(c.posts) >= 3, 20)

        # Producer stopped (session cap / crash) → a new hello restarts it.
        await prod.stop()
        assert prod.status.state == "stopped" and not prod.running
    async with websockets.connect(url) as ws:
        c = Client(ws)
        await c.send(type="hello", platforms=["fake"], buffer_target=40)
        await c.recv_until(lambda m: m["type"] == "session", 10)
        await wait_for(lambda: prod.running and prod.status.state in SCROLLING, 15)


def test_build_hub_laptop_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"stream": {"platforms": ["instagram"]}}))
    monkeypatch.setattr(stream_api, "CONFIG_PATH", cfg)
    monkeypatch.delenv("VS_STREAM_FAKE_URL", raising=False)
    ws = tmp_path / "ws"
    monkeypatch.setenv("VS_STREAM_PLATFORMS", "twitter,threads")
    monkeypatch.setenv("VS_STREAM_WORKSPACE", str(ws))
    monkeypatch.setenv("VS_STREAM_HOST_MODE", "laptop")

    hub = stream_api.build_hub_from_config()
    assert list(hub.adapters) == ["x", "threads"]  # CLI flag beats config.json
    assert hub.pool.profiles_dir == ws.resolve() / "profiles"
    assert hub.pool.channel == "chrome"
    assert hub.session_dir is not None and hub.session_dir.parent == ws.resolve() / "streams"
    assert hub.config.host_mode == "laptop"
    assert hub.config.login_hint == stream_api.LAPTOP_LOGIN_HINT
    assert hub.config.producer_overrides["x"].login_hint == stream_api.LAPTOP_LOGIN_HINT
    if hub.event_log:
        hub.event_log.close()

    monkeypatch.setenv("VS_STREAM_PLATFORMS", "")
    assert stream_api.configured_platforms() == []
    monkeypatch.delenv("VS_STREAM_PLATFORMS")
    assert stream_api.configured_platforms() == ["instagram"]


def test_run_stream_host_platform_flag():
    import argparse

    from scripts.run_stream_host import parse_platforms

    assert parse_platforms("x, Threads") == ["x", "threads"]
    assert parse_platforms("twitter") == ["x"]
    assert parse_platforms("") == []
    with pytest.raises(argparse.ArgumentTypeError):
        parse_platforms("x,myspace")

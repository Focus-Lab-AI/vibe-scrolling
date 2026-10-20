"""End-to-end test of the phone PWA against scripts/mock_stream_server.py.

Starts the mock host (uvicorn in a thread, random port), drives headless
Chromium at an iPhone-ish viewport, and checks: cards stream in, scrolling
pulls more via `demand`, the comments sheet shows collapsed + hidden replies,
the live-view login flow works (frames + not_implemented), and there are no
console errors. Screenshots land in tests/stream/artifacts/.
"""

from __future__ import annotations

import importlib.util
import socket
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"

playwright_sync = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")


def _load_mock():
    spec = importlib.util.spec_from_file_location("mock_stream_server", ROOT / "scripts" / "mock_stream_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mock_server():
    mod = _load_mock()
    app = mod.create_app()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("mock server did not start")
        time.sleep(0.05)
    yield app, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # pragma: no cover - env without chromium
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture()
def phone(browser, mock_server):
    app, base = mock_server
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
    )
    page = ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto(f"{base}/phone/?token=test-token")
    yield page, app, errors
    ctx.close()


def _count(page) -> int:
    return page.locator("#feed > .post").count()


def test_phone_stream_comments_and_live(phone):
    page, app, errors = phone
    ARTIFACTS.mkdir(exist_ok=True)

    # Token is persisted and scrubbed from the URL.
    page.wait_for_function("document.querySelectorAll('#feed > .post').length >= 10", timeout=15000)
    assert page.evaluate("localStorage.getItem('focuslab:stream:token')") == "test-token"
    assert "token=" not in page.url

    # Status strip + login banner from session/status events.
    page.wait_for_selector(".chip[data-platform='x']")
    assert page.locator("#login-banner [data-act='login']").count() == 2
    assert "log in on the host" in page.locator("#login-banner").inner_text()
    assert page.locator("#conn").get_attribute("data-state") == "live"

    # Backpressure: host stops at the high watermark until we scroll.
    page.wait_for_timeout(1500)
    settled = _count(page)
    assert 10 <= settled <= 26, settled
    hello = [m for m in app.state.messages if m.get("type") == "hello"]
    assert hello and hello[0]["buffer_target"] == 15

    page.screenshot(path=str(ARTIFACTS / "phone.png"))
    page.emulate_media(color_scheme="dark")
    page.screenshot(path=str(ARTIFACTS / "phone-dark.png"))
    page.emulate_media(color_scheme="light")

    # Curator "why" pill expands.
    first = page.locator("#feed > .post").first
    first.locator(".why-pill").click()
    assert first.locator(".why-box").is_visible()

    # Scroll to the bottom → demand → more cards.
    demands_before = len(app.state.demands)
    for _ in range(6):
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(400)
    page.wait_for_function(f"document.querySelectorAll('#feed > .post').length > {settled}", timeout=10000)
    assert len(app.state.demands) > demands_before
    assert any(d.get("last_seen_post_id") for d in app.state.demands)

    # No duplicate ids.
    ids = page.eval_on_selector_all("#feed > .post", "els => els.map(e => e.dataset.id)")
    assert len(ids) == len(set(ids))

    # Comments: spinner → thread with collapsed + hidden counts.
    page.evaluate("window.scrollTo(0, 0)")
    page.locator("#feed > .post").first.locator(".comments-btn").click()
    page.wait_for_selector("#sheet:not([hidden])")
    page.wait_for_selector("#sheet-body .reply", timeout=10000)
    collapsed = page.locator("#sheet-body .reply-collapsed")
    assert collapsed.count() == 2
    assert "Hidden by your agent" in collapsed.first.inner_text()
    footer = page.locator("#sheet-body .hidden-footer")
    assert footer.inner_text().startswith("2 hidden")
    collapsed.first.locator(".reply-reveal").click()
    assert collapsed.first.locator(".reply-inner").is_visible()
    page.screenshot(path=str(ARTIFACTS / "phone-comments.png"))
    page.locator("#sheet-close").click()
    assert page.locator("#sheet").is_hidden()

    # Live view, platform that streams frames: canvas gets painted, tap is sent.
    page.locator("#login-banner [data-platform='instagram']").click()
    page.wait_for_selector("#live:not([hidden])")
    page.wait_for_function("document.getElementById('live-msg').hidden === true", timeout=10000)
    assert page.evaluate("document.getElementById('live-canvas').width") == 270
    box = page.locator("#live-canvas").bounding_box()
    page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] / 4)
    page.wait_for_timeout(300)
    taps = [m for m in app.state.live_inputs if m.get("kind") == "tap"]
    assert taps and abs(taps[-1]["x"] - 0.5) < 0.05 and abs(taps[-1]["y"] - 0.25) < 0.05
    page.screenshot(path=str(ARTIFACTS / "phone-live.png"))
    page.locator("#live-close").click()
    assert page.locator("#live").is_hidden()
    assert any(m.get("type") == "live.stop" for m in app.state.messages)

    # Live view, platform where the host says not_implemented: friendly message.
    page.locator("#login-banner [data-platform='linkedin']").click()
    page.wait_for_selector("#live-msg h3")
    assert "coming soon" in page.locator("#live-msg").inner_text().lower()
    page.locator("#live-msg [data-act='live-close']").click()
    assert page.locator("#live").is_hidden()

    assert not errors, errors


def test_phone_reconnects_and_dedupes(phone, mock_server):
    page, app, errors = phone
    page.wait_for_function("document.querySelectorAll('#feed > .post').length >= 5", timeout=15000)
    hellos_before = sum(1 for m in app.state.messages if m.get("type") == "hello")
    # Simulate a dropped socket; the client should back off, reconnect and re-hello.
    page.evaluate("window.PhoneClient.state.ws.close()")
    page.wait_for_function("document.getElementById('conn').dataset.state === 'live'", timeout=10000)
    page.wait_for_timeout(300)
    hellos_after = sum(1 for m in app.state.messages if m.get("type") == "hello")
    assert hellos_after > hellos_before
    ids = page.eval_on_selector_all("#feed > .post", "els => els.map(e => e.dataset.id)")
    assert len(ids) == len(set(ids))
    assert not errors, errors

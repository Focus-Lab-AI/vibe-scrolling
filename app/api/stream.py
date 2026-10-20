"""Streaming host API: WebSocket `/ws/stream` + `GET /api/stream/status`.

Wire protocol: docs/streaming-plan.md. The StreamHub is a lazily created
app-level singleton (tests inject one with `set_hub`).

Config (config.json → "stream", all optional):
    {"platforms": ["x", ...],          # opt-in list of real platforms
     "token": "...",                   # or env VS_STREAM_TOKEN
     "headless": false,                # or env VS_STREAM_HEADLESS=1
     "curator": "heuristic",           # make_curator kind
     "low_watermark": 8, "high_watermark": 20,
     "batch_window": 1.5, "session_cap_minutes": 30,
     "profiles_dir": "<DATA_DIR>/profiles"}
Env VS_STREAM_FAKE_URL=<url> enables the local fake platform for dev.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.paths import CONFIG_PATH, DATA_DIR, SESSION_DIR, get_workspace_dir
from src.stream.contracts import to_wire

_hub = None
_hub_lock: Optional[asyncio.Lock] = None


@asynccontextmanager
async def _lifespan(app):
    yield
    await shutdown_hub()


router = APIRouter(lifespan=_lifespan)


# ---------------------------------------------------------------------------
# config + hub construction
# ---------------------------------------------------------------------------

def _read_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def stream_config() -> dict:
    return _read_config().get("stream") or {}


def stream_token() -> Optional[str]:
    cfg = _read_config()
    return (
        os.environ.get("VS_STREAM_TOKEN")
        or (cfg.get("stream") or {}).get("token")
        or cfg.get("stream_token")
        or None
    )


def configured_platforms() -> list[str]:
    names = [p for p in (stream_config().get("platforms") or []) if p != "fake"]
    if os.environ.get("VS_STREAM_FAKE_URL"):
        names.append("fake")
    return names


def _resolve(p: str | Path) -> Path:
    p = Path(p).expanduser()
    return p if p.is_absolute() else DATA_DIR / p


def build_hub_from_config():
    from src.stream.adapters import build_adapter
    from src.stream.browser_pool import BrowserPool
    from src.stream.curator import load_curator_context, make_curator
    from src.stream.hub import HubConfig, StreamHub
    from src.stream.producer import ProducerConfig

    cfg = _read_config()
    scfg = cfg.get("stream") or {}
    platform_cfg = cfg.get("platforms") or {}

    adapters = {}
    for name in configured_platforms():
        kwargs = {"base_url": os.environ["VS_STREAM_FAKE_URL"]} if name == "fake" else {}
        adapter = build_adapter(name, **kwargs)
        if adapter is not None:
            adapters[name] = adapter

    base = ProducerConfig(session_cap_minutes=float(scfg.get("session_cap_minutes", 30)))
    overrides = {}
    for name in adapters:
        pc = platform_cfg.get(name) or {}
        if name == "fake":
            lo, hi = float(os.environ.get("VS_STREAM_FAKE_DELAY", "0.4")), float(os.environ.get("VS_STREAM_FAKE_DELAY", "0.4")) * 3
        else:
            lo, hi = float(pc.get("scroll_delay_min", 2)), float(pc.get("scroll_delay_max", 5))
        overrides[name] = ProducerConfig(
            scroll_delay_min=lo, scroll_delay_max=max(lo, hi),
            session_cap_minutes=base.session_cap_minutes,
        )

    session_files = {
        name: _resolve(pc["session_file"])
        for name, pc in platform_cfg.items()
        if isinstance(pc, dict) and pc.get("session_file")
    }
    headless_env = os.environ.get("VS_STREAM_HEADLESS")
    headless = headless_env not in (None, "", "0", "false") if headless_env is not None else bool(scfg.get("headless", False))
    pool = BrowserPool(
        profiles_dir=_resolve(scfg.get("profiles_dir") or "profiles"),
        session_dir=SESSION_DIR,
        session_files=session_files,
        headless=headless,
    )

    try:
        curator = make_curator(scfg.get("curator", "heuristic"))
    except Exception as e:  # noqa: BLE001
        print(f"[stream] curator {scfg.get('curator')!r} unavailable ({e}); using heuristic")
        curator = make_curator("heuristic")

    workspace = get_workspace_dir()
    ctx = None
    if workspace is not None:
        try:
            ctx = load_curator_context(workspace, allow_template=True)
        except Exception as e:  # noqa: BLE001
            print(f"[stream] could not load goals: {e}")
    streams_dir = (workspace or DATA_DIR) / "streams"

    hub_cfg = HubConfig(
        low_watermark=int(scfg.get("low_watermark", 8)),
        high_watermark=int(scfg.get("high_watermark", 20)),
        batch_window=float(scfg.get("batch_window", 1.5)),
        producer=base,
        producer_overrides=overrides,
    )
    return StreamHub(
        pool, adapters, curator, ctx=ctx, config=hub_cfg,
        streams_dir=streams_dir, workspace_dir=workspace,
    )


async def get_hub():
    global _hub, _hub_lock
    if _hub is not None:
        return _hub
    if _hub_lock is None:
        _hub_lock = asyncio.Lock()
    async with _hub_lock:
        if _hub is None:
            _hub = build_hub_from_config()
    return _hub


def set_hub(hub) -> None:
    """Inject a hub (tests / custom hosts). Pass None to reset."""
    global _hub
    _hub = hub


async def shutdown_hub() -> None:
    global _hub
    hub, _hub = _hub, None
    if hub is not None:
        try:
            if hub.session_dir is not None and hub.event_log and hub.event_log.count:
                hub.write_filtered()
        except Exception as e:  # noqa: BLE001
            print(f"[stream] write_filtered on shutdown failed: {e}")
        await hub.close(close_pool=True)


def _authorized(token: Optional[str]) -> bool:
    expected = stream_token()
    return not expected or secrets.compare_digest(token or "", expected)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

@router.get("/api/stream/status")
async def stream_status(request: Request, token: Optional[str] = None):
    if not _authorized(token or request.headers.get("x-stream-token")):
        raise HTTPException(401, "invalid stream token")
    if _hub is None:
        return {"running": False, "configured_platforms": configured_platforms()}
    return {
        "running": _hub.started,
        "configured_platforms": list(_hub.adapters),
        "session": _hub.session_message(),
        "status": _hub.status_message(),
    }


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

def _error(message: str, request_id: Optional[str] = None) -> dict:
    out = {"type": "error", "message": message}
    if request_id is not None:
        out["request_id"] = request_id
    return out


async def _writer(ws: WebSocket, sub) -> None:
    while True:
        msg = await sub.queue.get()
        await ws.send_json(msg)


async def _open_comments(hub, sub, msg: dict) -> None:
    request_id = msg.get("request_id")
    post_id = msg.get("post_id")
    if not post_id:
        sub.send(_error("open_comments requires post_id", request_id))
        return
    try:
        thread = await hub.open_comments(str(post_id), request_id)
        sub.send(to_wire(thread))
    except KeyError as e:
        sub.send(_error(str(e).strip("'\""), request_id))
    except Exception as e:  # noqa: BLE001
        sub.send(_error(f"open_comments failed: {e}", request_id))


@router.websocket("/ws/stream")
async def ws_stream(ws: WebSocket):
    if not _authorized(ws.query_params.get("token")):
        await ws.close(code=4401)
        return
    await ws.accept()
    hub = None
    sub = None
    tasks: set[asyncio.Task] = set()
    writer: Optional[asyncio.Task] = None

    async def reply(msg: dict) -> None:
        if sub is not None:
            sub.send(msg)
        else:
            await ws.send_json(msg)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    raise ValueError("not an object")
            except ValueError:
                await reply(_error("invalid JSON message"))
                continue
            mtype = msg.get("type")
            request_id = msg.get("request_id")

            if mtype == "hello":
                hub = await get_hub()
                target = msg.get("buffer_target")
                platforms = msg.get("platforms") or None
                await hub.start(platforms)
                greeting = [hub.session_message(), hub.status_message()]
                if not hub.adapters:
                    greeting.insert(0, _error(
                        "no stream platforms configured (config.json stream.platforms or VS_STREAM_FAKE_URL)"))
                if sub is None:
                    sub = hub.subscribe(
                        int(target) if target else None,
                        debug=bool(msg.get("debug")),
                        initial=greeting,
                    )
                    writer = asyncio.create_task(_writer(ws, sub))
                else:
                    if target:
                        sub.buffer_target = int(target)
                    for g in greeting:
                        sub.send(g)
                sub.platforms = platforms
                hub.update_demand(sub, int(msg.get("buffer_depth") or 0))
            elif sub is None or hub is None:
                await reply(_error("send hello first", request_id))
            elif mtype == "demand":
                hub.update_demand(sub, int(msg.get("buffer_depth") or 0), msg.get("last_seen_post_id"))
            elif mtype == "open_comments":
                t = asyncio.create_task(_open_comments(hub, sub, msg))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            elif isinstance(mtype, str) and mtype.startswith("live."):
                sub.send(_error("not_implemented", request_id))
            elif mtype == "feedback":
                hub.record({"type": "feedback", "post_id": msg.get("post_id"), "signal": msg.get("signal")})
            elif mtype == "status":
                sub.send(hub.status_message())
            else:
                sub.send(_error(f"unknown message type {mtype!r}", request_id))
    except WebSocketDisconnect:
        pass
    except RuntimeError as e:  # socket closed under us
        print(f"[stream] ws closed: {e}")
    finally:
        if hub is not None and sub is not None:
            hub.unsubscribe(sub)
        for t in list(tasks) + ([writer] if writer else []):
            t.cancel()

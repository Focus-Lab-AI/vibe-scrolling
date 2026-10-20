#!/usr/bin/env python3
"""Run the streaming host (FastAPI app + /ws/stream + /phone) for dev.

    .venv/bin/python scripts/run_stream_host.py --fake            # fake platform only
    .venv/bin/python scripts/run_stream_host.py --fake --headless
    .venv/bin/python scripts/run_stream_host.py --port 8765 --token s3cret

Real platforms are opt-in via config.json → "stream": {"platforms": ["x", ...]}.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


async def main(args: argparse.Namespace) -> None:
    import uvicorn

    site = None
    if args.fake:
        from tests.stream.fake_site import FakeSite

        site = FakeSite()
        await site.start(port=args.fake_port)
        os.environ["VS_STREAM_FAKE_URL"] = site.url
        os.environ.setdefault("VS_STREAM_FAKE_DELAY", str(args.fake_delay))
        print(f"[stream] fake site at {site.url}")
    if args.headless:
        os.environ["VS_STREAM_HEADLESS"] = "1"
    if args.token:
        os.environ["VS_STREAM_TOKEN"] = args.token

    from app.paths import initialize

    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    initialize()  # sets PLAYWRIGHT_BROWSERS_PATH to the app cache dir...
    if browsers_path:  # ...but keep an explicitly configured one (CI / dev boxes)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
    from app.server import create_app

    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    )
    token = os.environ.get("VS_STREAM_TOKEN")
    host = _lan_ip() if args.host in ("0.0.0.0", "::") else args.host
    query = f"?token={token}" if token else ""
    print(f"[stream] phone URL: http://{host}:{args.port}/phone/{query}")
    print(f"[stream] status:    http://{host}:{args.port}/api/stream/status{query}")
    try:
        await server.serve()
    finally:
        if site is not None:
            await site.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--fake", action="store_true", help="serve the fake feed site and enable the fake platform")
    p.add_argument("--fake-port", type=int, default=0)
    p.add_argument("--fake-delay", type=float, default=0.4, help="min scroll delay for the fake platform (s)")
    p.add_argument("--headless", action="store_true", help="headless browsers (default: headed, Xvfb on Linux)")
    p.add_argument("--token", default=None, help="require ?token= on /ws/stream")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass

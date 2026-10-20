#!/usr/bin/env python3
"""Run the streaming host (FastAPI app + /ws/stream + /phone) for dev.

    .venv/bin/python scripts/run_stream_host.py --fake            # fake platform only
    .venv/bin/python scripts/run_stream_host.py --fake --headless
    .venv/bin/python scripts/run_stream_host.py --port 8765 --token s3cret
    .venv/bin/python scripts/run_stream_host.py --laptop --platforms x,threads --qr

Real platforms come from --platforms, else config.json → "stream": {"platforms": [...]}.
On a laptop use scripts/run_laptop.sh, which sets up the venv and passes --laptop --qr.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REAL_PLATFORMS = ["x", "threads", "instagram", "youtube", "linkedin"]


def _lan_ip() -> str:
    if sys.platform == "darwin":  # Wi-Fi is en0 on most Macs, en1 on some
        for iface in ("en0", "en1"):
            try:
                out = subprocess.run(
                    ["ipconfig", "getifaddr", iface], capture_output=True, text=True, timeout=3
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                out = ""
            if out:
                return out
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _print_qr(url: str) -> None:
    try:
        import qrcode
    except ImportError:
        print("[stream] (pip install qrcode to get a scannable QR code here)")
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def parse_platforms(value: str) -> list[str]:
    names = [p.strip().lower() for p in value.split(",") if p.strip()]
    names = ["x" if p == "twitter" else p for p in names]
    bad = [p for p in names if p not in REAL_PLATFORMS]
    if bad:
        raise argparse.ArgumentTypeError(
            f"unknown platform(s) {', '.join(bad)}; choose from {','.join(REAL_PLATFORMS)}"
        )
    return names


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
    if args.platforms is not None:
        os.environ["VS_STREAM_PLATFORMS"] = ",".join(args.platforms)
    if args.workspace:
        os.environ["VS_STREAM_WORKSPACE"] = str(Path(args.workspace).expanduser())
    if args.laptop:
        os.environ["VS_STREAM_HOST_MODE"] = "laptop"
    if args.curator:
        os.environ["VS_STREAM_CURATOR"] = args.curator

    from app.paths import initialize

    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    initialize()  # sets PLAYWRIGHT_BROWSERS_PATH to the app cache dir...
    if browsers_path:  # ...but keep an explicitly configured one (CI / dev boxes)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
    from app.api.stream import configured_platforms, stream_workspace
    from app.server import create_app

    if not configured_platforms():
        print("[stream] WARNING: no platforms configured — pass --platforms x,threads or --fake")
    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    )
    token = os.environ.get("VS_STREAM_TOKEN")
    host = _lan_ip() if args.host in ("0.0.0.0", "::") else args.host
    query = f"?token={token}" if token else ""
    phone_url = f"http://{host}:{args.port}/phone/{query}"

    serve = asyncio.create_task(server.serve())
    while not server.started and not serve.done():
        await asyncio.sleep(0.05)
    try:
        if server.started:
            ws = stream_workspace()
            print()
            print(f"[stream] platforms: {', '.join(configured_platforms()) or '(none)'}")
            print(f"[stream] workspace: {ws or '(none — using app data dir)'}")
            print(f"[stream] status:    http://{host}:{args.port}/api/stream/status{query}")
            if args.qr:
                print("\nScan with your phone camera (same Wi-Fi as this computer):\n")
                _print_qr(phone_url)
            print(f"\n[stream] phone URL: {phone_url}")
            if args.laptop:
                print("[stream] When a platform needs a login, a browser window opens here —")
                print("[stream] sign in there and the feed starts streaming to your phone.")
            print("[stream] Ctrl+C to stop.\n")
        await serve
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
    p.add_argument("--platforms", type=parse_platforms, default=None,
                   help=f"comma-separated real platforms ({','.join(REAL_PLATFORMS)}); overrides config.json")
    p.add_argument("--headless", action="store_true", help="headless browsers (default: headed, Xvfb on headless Linux)")
    p.add_argument("--token", default=None, help="require ?token= on /ws/stream")
    p.add_argument("--workspace", default=None, help="workspace dir (browser profiles, streams, goals.md)")
    p.add_argument("--curator", default=None, help="heuristic | claude | cli (overrides config.json)")
    p.add_argument("--laptop", action="store_true", help="feed browsers are visible windows on this machine")
    p.add_argument("--qr", action="store_true", help="print the phone URL as a terminal QR code")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass

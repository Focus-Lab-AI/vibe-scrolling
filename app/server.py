"""FastAPI app factory with static file mounts and CORS."""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import auth, collection, config, curated, data, setup, stream, workspace
from app.paths import FEED_DATA_DIR, IS_BUNDLED, STATIC_DIR, get_collected_data_dir
from src.storage import migrate_legacy_runs


class NoCacheMiddleware(BaseHTTPMiddleware):
    """Dev-only: tell WebKit not to cache anything, so Cmd+R in pywebview
    shows current JS/CSS/HTML without a full app restart. Disabled in
    bundled mode so the shipped .app benefits from normal HTTP caching."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="Focus Lab — Vibe Scrolling")

    # Migrate old flat run directories to new date/job/platform hierarchy
    migrate_legacy_runs(str(FEED_DATA_DIR))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if not IS_BUNDLED:
        app.add_middleware(NoCacheMiddleware)

    # API routers
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(collection.router, prefix="/api/collection", tags=["collection"])
    app.include_router(config.router, prefix="/api/config", tags=["config"])
    app.include_router(data.router, prefix="/api/data", tags=["data"])
    app.include_router(setup.router, prefix="/api/setup", tags=["setup"])
    app.include_router(workspace.router, prefix="/api/workspace", tags=["workspace"])
    app.include_router(curated.router, prefix="/api/curated", tags=["curated"])
    # Streaming host: WS /ws/stream + /api/stream/* (paths are absolute in the router)
    app.include_router(stream.router, tags=["stream"])

    # Serve collected data (media files, JSON). Resolved per-request because
    # the active data dir flips when the user picks a workspace at runtime —
    # a static mount captured at boot would point at the wrong dir.
    @app.get("/feed_data/{path:path}")
    async def serve_data(path: str):
        return _serve_under(get_collected_data_dir(), path)

    # Phone PWA for the streaming host (before the SPA catch-all)
    app.mount(
        "/phone",
        StaticFiles(directory=str(STATIC_DIR / "phone"), html=True, check_dir=False),
        name="phone",
    )

    # Serve frontend SPA (must be last — catches all remaining paths)
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


def _serve_under(root: Path, path: str) -> FileResponse:
    """Serve `root/path` as a file, blocking traversal outside root."""
    target = (root / path).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise HTTPException(403, "Path traversal blocked")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, f"Not found: {path}")
    return FileResponse(target)

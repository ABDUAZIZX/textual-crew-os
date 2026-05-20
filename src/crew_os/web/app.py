"""FastAPI application factory for the local dashboard.

The app is meant to be served on loopback only (enforced by
:class:`crew_os.config.Settings`). CORS is locked to localhost origins
and GET-only, since the dashboard is read-only.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from crew_os.memory.sqlite_store import SqliteStore
from crew_os.metrics.sampler import MetricsSampler
from crew_os.metrics.usage import UsageTracker
from crew_os.orchestration.bus import MessageBus
from crew_os.orchestration.registry import AgentRegistry
from crew_os.web.routes import router as rest_router
from crew_os.web.ws import websocket_endpoint

_LOCALHOST_ORIGINS = [
    "http://127.0.0.1:8765",
    "http://localhost:8765",
]

_WEB_DIR = Path(__file__).parent
_STATIC_DIR = _WEB_DIR / "static"
_TEMPLATE = _WEB_DIR / "templates" / "dashboard.html"


def create_app(
    *,
    registry: AgentRegistry,
    store: SqliteStore,
    bus: MessageBus,
    sampler: MetricsSampler,
    usage: UsageTracker | None = None,
    extra_origins: list[str] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await sampler.start()
        try:
            yield
        finally:
            await sampler.stop()

    app = FastAPI(title="Crew Control Center", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_LOCALHOST_ORIGINS + (extra_origins or []),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    app.state.registry = registry
    app.state.store = store
    app.state.bus = bus
    app.state.sampler = sampler
    app.state.usage = usage if usage is not None else UsageTracker()
    app.state.start_time = time.monotonic()

    app.include_router(rest_router)
    app.add_api_websocket_route("/ws", websocket_endpoint)

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(_TEMPLATE)

    return app

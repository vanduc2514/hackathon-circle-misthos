from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from misthos.api.router import api_router
from misthos.config import settings
from misthos.observability import logs
from misthos.observability.middleware import RequestContext
from misthos.store import store
from misthos.workers.sweeper import run_forever

DESCRIPTION = """
Marketplace where a company or a maintainer puts a **fixed price** on a GitHub
issue and pays whoever fixes it, settled in USDC on Arc.

This build is **simulated**. The lifecycle is real code with real state
transitions, and the prices come from the actual pricing engine, but the money is
fake: no chain is contacted. GitHub is simulated too until a GitHub App is
configured, and then pull request events drive the lifecycle. State is in memory
unless `MISTHOS_DATABASE_URL` points at a database, and a sweeper applies claim
expiry, deadline refunds and the silent-publisher release on its own.

Everything lives under `/api/v1`.
"""


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Run the sweeper beside the API unless a dedicated worker process owns it."""
    logs.configure(json_lines=settings.log_json, level=settings.log_level)
    sweeper = (
        asyncio.create_task(run_forever(store, settings.sweep_interval_seconds))
        if settings.sweeper_in_process
        else None
    )
    try:
        yield
    finally:
        if sweeper is not None:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=DESCRIPTION,
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Idempotent-Replayed", "Retry-After"],
    )
    # Outermost, so the correlation id covers everything, CORS included.
    app.add_middleware(RequestContext)
    app.include_router(api_router)

    @app.get("/internal/metrics", include_in_schema=False)
    async def prometheus() -> Response:
        """For the metrics scraper. Not proxied by the web app; keep it off the
        public internet."""
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "docs": "/docs",
            "api": "/api/v1",
            "health": "/api/v1/health",
        }

    return app


app = create_app()

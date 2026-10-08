from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from misthos.api.router import api_router
from misthos.api.v1.issues import already_published
from misthos.auth import sessions
from misthos.config import settings
from misthos.observability import logs
from misthos.observability.metrics import exported
from misthos.observability.middleware import RequestContext
from misthos.services.attestor import (
    assert_key_is_not_in_the_environment,
    assert_secrets_are_configured,
)
from misthos.store import AlreadyListed, store
from misthos.workers.sweeper import run_forever

log = logging.getLogger("misthos.main")

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
    sessions.warn_if_unshared()
    await report_settlement_problems()
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


async def report_settlement_problems() -> list[str]:
    """Say at startup what would stop the escrow paying a funded issue out, such as no
    fee recipient on it (#127). Funding refuses on the same check, so this is the
    operator hearing of it before a publisher does; it does not stop the API, which
    still serves everything that moves no money."""
    if settings.simulated:
        return []
    problems = await asyncio.to_thread(store.chain.settlement_problems)
    for problem in problems:
        log.error("settlement is not ready: %s", problem, extra={"alert": "settlement"})
    return problems


def create_app() -> FastAPI:
    # uvicorn sets up its own loggers, imports the app, then writes its first line.
    # Configured only at startup, "Started server process" and "Waiting for
    # application startup." stayed plain text in a JSON log, so when uvicorn is the
    # importer its loggers are taken over now. Anyone else importing the app, a test
    # included, is left alone until startup.
    if logs.uvicorn_configured():
        logs.configure(json_lines=settings.log_json, level=settings.log_level)
    # Key material in the environment means the boundary has already been crossed,
    # so the process refuses to start rather than serving with a key an agent can read.
    assert_key_is_not_in_the_environment()
    assert_secrets_are_configured(
        settings.simulated, settings.attestor_secret_ref, settings.owner_secret_ref
    )

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
    app.add_exception_handler(AlreadyListed, already_published)

    @app.get("/internal/metrics", include_in_schema=False)
    async def prometheus() -> Response:
        """For the metrics scraper. Not proxied by the web app; keep it off the
        public internet. The sweeper's series are here only when the sweeper runs in
        this process; otherwise the worker serves them."""
        registry = exported(sweeper=settings.sweeper_in_process)
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

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

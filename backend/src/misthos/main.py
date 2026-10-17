from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from misthos.api.router import api_router
from misthos.api.v1.issues import already_published
from misthos.api.v1.webhooks import assert_webhook_secret_is_configured
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

DESCRIPTION = """
Marketplace where a company or a maintainer puts a **fixed price** on a GitHub
issue and pays whoever fixes it, settled in USDC on Arc.

This build settles on Arc. The lifecycle is real code with real state
transitions, the prices come from the actual pricing engine, and the money moves
through `MisthosEscrow`. GitHub drives the lifecycle once the App is configured.
State is in memory unless `MISTHOS_DATABASE_URL` points at a database, and a
sweeper applies claim expiry, deadline refunds and the silent-publisher release on
its own.

Everything lives under `/api/v1`.
"""


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Run the sweeper beside the API unless a dedicated worker process owns it."""
    logs.configure(json_lines=settings.log_json, level=settings.log_level)
    sessions.warn_if_unshared()
    # Build the store now: a deployment missing the escrow, the GitHub App or the
    # signing keys fails at startup rather than on the first request.
    store.current()
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


log = logging.getLogger("misthos.main")

_legacy_switch_warned = False


def warn_legacy_switch(env: Mapping[str, str] | None = None) -> str | None:
    """Say once that `MISTHOS_SIMULATED` no longer chooses anything.

    The switch is gone, so a deployment that still sets it runs live. Silently
    ignoring it would leave an operator believing they had turned money off, so the
    process names it at startup. The publisher's call was to log rather than refuse,
    which keeps an effective deployment booting through the change.
    """
    global _legacy_switch_warned
    value = (env if env is not None else os.environ).get("MISTHOS_SIMULATED")
    if value is None:
        return None
    message = (
        "MISTHOS_SIMULATED is set but simulation has been removed; "
        "the service runs live and the variable is ignored"
    )
    if not _legacy_switch_warned:
        log.warning(message, extra={"alert": "config"})
        _legacy_switch_warned = True
    return message


async def report_settlement_problems() -> list[str]:
    """Say at startup what would stop the escrow paying a funded issue out, such as no
    fee recipient on it (#127). Read here so an operator hears of it before a publisher
    commits money, rather than at the first release; it does not stop the API, which
    still serves everything that moves no money."""
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
    warn_legacy_switch()
    assert_secrets_are_configured(settings.attestor_secret_ref, settings.owner_secret_ref)
    # A live App whose deliveries could never be verified is a deployment that looks
    # configured and does nothing, so it is refused where it can still be fixed.
    assert_webhook_secret_is_configured()

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

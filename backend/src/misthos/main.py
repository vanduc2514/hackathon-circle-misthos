from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from misthos.api.router import api_router
from misthos.config import settings

DESCRIPTION = """
Marketplace where a company or a maintainer puts a **fixed price** on a GitHub
issue and pays whoever fixes it, settled in USDC on Arc.

This build is **simulated**. The lifecycle is real code with real state
transitions, and the prices come from the actual pricing engine, but the money
and the GitHub calls are fake. No chain is contacted.

Everything lives under `/api/v1`.
"""


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=DESCRIPTION,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)

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

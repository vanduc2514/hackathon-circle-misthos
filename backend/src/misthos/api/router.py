from __future__ import annotations

from fastapi import APIRouter

from misthos.api.v1 import (
    auth,
    catalog,
    contributors,
    issues,
    paid,
    publishers,
    wallets,
    webhooks,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(issues.router)
api_router.include_router(contributors.router)
api_router.include_router(publishers.router)
api_router.include_router(webhooks.router)
api_router.include_router(wallets.router)
api_router.include_router(catalog.router)
# The edge's side door for x402 calls (#200): token-gated, and not in the schema.
api_router.include_router(paid.router, include_in_schema=False)

from __future__ import annotations

from fastapi import APIRouter

from misthos.api.v1 import auth, contributors, issues, plans, publishers, wallets, webhooks

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(issues.router)
api_router.include_router(contributors.router)
api_router.include_router(publishers.router)
api_router.include_router(plans.router)
api_router.include_router(webhooks.router)
api_router.include_router(wallets.router)

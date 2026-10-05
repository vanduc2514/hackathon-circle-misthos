from __future__ import annotations

from fastapi import APIRouter

from misthos.api.v1 import issues, webhooks

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(issues.router)
api_router.include_router(webhooks.router)

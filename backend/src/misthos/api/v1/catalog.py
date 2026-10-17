"""What the API sells: the catalog behind the web app's API page (#208).

The free routes are in `openapi.json`, marked `x-misthos-public`. This route adds the
x402 routes the edge sells, read from the edge at the prices it charges.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from misthos.api.public import public
from misthos.schemas import PaidCatalogOut
from misthos.services import paid_catalog

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get(
    "/paid",
    response_model=PaidCatalogOut,
    response_model_by_alias=True,
    openapi_extra=public(
        "Core",
        "Paid routes",
        "Every x402 route the edge sells, its price, and where and how to pay.",
    ),
)
async def paid() -> PaidCatalogOut:
    return await run_in_threadpool(paid_catalog.catalog)

"""An organisation's own controls and records: its spending policy, its spend, and
its audit export (epic #13).

These are the organisation's alone, and organisations cannot sign in yet (#70), so
every route here is served by the simulation only; an operator exports the audit
record with `python -m misthos.services.audit`.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool

from misthos.config import settings
from misthos.domain.policy import PolicyRefusal
from misthos.schemas import AuditExport, PolicyRequest, Publisher, SpendOut
from misthos.services.audit import export, to_csv
from misthos.store import store

router = APIRouter(prefix="/publishers", tags=["publishers"])


def _simulation_only(what: str) -> None:
    if not settings.simulated:
        raise HTTPException(
            status_code=403, detail=f"{what} is served to the organisation once sign-in exists"
        )


@router.put("/{publisher_id}/policy", response_model=Publisher)
async def set_policy(publisher_id: str, payload: PolicyRequest) -> Publisher:
    """Replace the organisation's spending policy: a release threshold with its named
    approvers, and monthly limits per issue label."""
    _simulation_only("the spending policy")
    try:
        return await run_in_threadpool(
            lambda: store.set_policy(
                publisher_id,
                approval_threshold_usdc=payload.approval_threshold_usdc,
                approvers=payload.approvers,
                category_limits=payload.category_limits,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no publisher {publisher_id}") from exc
    except PolicyRefusal as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{publisher_id}/spend", response_model=SpendOut)
async def spend(publisher_id: str, year: int | None = None) -> SpendOut:
    """What was budgeted, committed, released and refunded in a year, by category,
    and the settled fixes to file for a security review."""
    _simulation_only("the spend view")
    try:
        return await run_in_threadpool(store.spend, publisher_id, year)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no publisher {publisher_id}") from exc


@router.get(
    "/{publisher_id}/audit",
    response_model=AuditExport,
    responses={200: {"content": {"text/csv": {}}}},
)
async def audit(
    publisher_id: str, format: Literal["json", "csv"] = Query(default="json")
) -> AuditExport | Response:
    """The decision record and the money events for every issue the organisation
    funded, unedited, as JSON or one sortable CSV."""
    _simulation_only("the audit export")
    found = await run_in_threadpool(export, store, publisher_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no publisher {publisher_id}")
    if format == "csv":
        return Response(
            content=to_csv(found),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="misthos-audit-{publisher_id}.csv"'
            },
        )
    return found

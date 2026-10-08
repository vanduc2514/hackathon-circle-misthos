"""An organisation's own controls and records: its spending policy, its spend, its
audit export (epic #13), and its money as the pricing engine sees it (#43).

These are the organisation's alone: a signed-in publisher sees and sets its own,
and the simulation lets the demo see any. An operator exports the audit record with
`python -m misthos.services.audit`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool

from misthos.api.session import SIGNED_IN, require_owner_or_simulation
from misthos.config import settings
from misthos.domain import plans
from misthos.domain.policy import PolicyRefusal
from misthos.schemas import (
    Account,
    AuditExport,
    FinanceOut,
    PolicyRequest,
    Publisher,
    RepositoriesOut,
    SpendOut,
)
from misthos.services.audit import export, to_csv
from misthos.store import store

router = APIRouter(prefix="/publishers", tags=["publishers"])


async def require_plan(publisher_id: str, feature: plans.Feature) -> None:
    """402 when the publisher's plan does not include the feature (06, the tiers)."""
    try:
        allowed = await run_in_threadpool(store.entitled, publisher_id, feature)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no publisher {publisher_id}") from exc
    if not allowed:
        raise HTTPException(status_code=402, detail=plans.refusal(feature))


@router.put("/{publisher_id}/policy", response_model=Publisher)
async def set_policy(
    publisher_id: str,
    payload: PolicyRequest,
    account: Account | None = SIGNED_IN,
) -> Publisher:
    """Replace the organisation's spending policy: a release threshold with its named
    approvers, and monthly limits per issue label."""
    require_owner_or_simulation(account, publisher_id, "set this spending policy")
    await require_plan(publisher_id, plans.Feature.POLICY)
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
async def spend(
    publisher_id: str,
    year: int | None = None,
    account: Account | None = SIGNED_IN,
) -> SpendOut:
    """What was budgeted, committed, released and refunded in a year, by category,
    and the settled fixes to file for a security review."""
    require_owner_or_simulation(account, publisher_id, "see this spend")
    await require_plan(publisher_id, plans.Feature.SPEND)
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
    publisher_id: str,
    format: Literal["json", "csv"] = Query(default="json"),
    account: Account | None = SIGNED_IN,
) -> AuditExport | Response:
    """The decision record and the money events for every issue the organisation
    funded, unedited, as JSON or one sortable CSV."""
    require_owner_or_simulation(account, publisher_id, "export this audit record")
    await require_plan(publisher_id, plans.Feature.AUDIT)
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


@router.get("/{publisher_id}/finance", response_model=FinanceOut)
async def finance(publisher_id: str, account: Account | None = SIGNED_IN) -> FinanceOut:
    """The declared budget, what the publisher's connected books say, and the lower of
    the two, which is what caps every price it is offered. Read-only, and the
    publisher's alone: cash and budgets are commercially sensitive."""
    require_owner_or_simulation(account, publisher_id, "see these books")
    try:
        publisher, read, problem = await run_in_threadpool(store.finance_context, publisher_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no publisher {publisher_id}") from exc
    # Plain decimals, like every other *_usdc string: the stored figure is formatted.
    declared_amount = Decimal(publisher.budget_remaining_usdc.replace(",", ""))
    declared = f"{declared_amount:.2f}"
    caps = declared
    if read is not None and read.budget_remaining is not None:
        caps = f"{min(declared_amount, read.budget_remaining.decimal):.2f}"
    return FinanceOut(
        publisher_id=publisher_id,
        declared_budget_usdc=declared,
        connected=read is not None or bool(problem),
        source=read.source if read else None,
        budget_remaining_usdc=f"{read.budget_remaining.decimal:.2f}"
        if read and read.budget_remaining is not None
        else None,
        cash_usdc=f"{read.cash.decimal:.2f}" if read and read.cash is not None else None,
        as_of=read.as_of if read else None,
        caps_prices_at_usdc=caps,
        note=problem or (read.note if read else ""),
    )


@router.get("/{publisher_id}/repositories", response_model=RepositoriesOut)
async def repositories(publisher_id: str, account: Account | None = SIGNED_IN) -> RepositoriesOut:
    """The repositories this publisher installed the GitHub App on (#6). An issue there
    given the label is priced without opening the web app."""
    require_owner_or_simulation(account, publisher_id, "see these repositories")
    if await run_in_threadpool(store.get_publisher, publisher_id) is None:
        raise HTTPException(status_code=404, detail=f"no publisher {publisher_id}")
    slug = settings.github_app_slug
    return RepositoriesOut(
        repositories=await run_in_threadpool(store.connections_of, publisher_id),
        label=settings.github_label,
        install_url=f"https://github.com/apps/{slug}/installations/new" if slug else None,
    )

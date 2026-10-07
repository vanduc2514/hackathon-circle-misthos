from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool

from misthos.api.session import SIGNED_IN, require_owner_or_simulation
from misthos.schemas import (
    Account,
    AnnualStatement,
    ContributorProfile,
    ReputationEventOut,
    money,
)
from misthos.services.statements import annual_statement, to_csv
from misthos.store import store

router = APIRouter(prefix="/contributors", tags=["contributors"])


@router.get("", response_model=list[ContributorProfile])
async def list_contributors() -> list[ContributorProfile]:
    """Public profiles. A wallet is never served next to a handle; see docs/PRIVACY.md."""
    contributors = await run_in_threadpool(store.list_contributors)
    return [ContributorProfile.of(c) for c in contributors]


@router.get("/{contributor_id}/reputation", response_model=list[ReputationEventOut])
async def reputation(contributor_id: str) -> list[ReputationEventOut]:
    """What a contributor's reputation is made of: one event per settled issue, from
    the money ledger and nothing else. Public, like the settlement comment it mirrors."""
    if await run_in_threadpool(store.get_contributor, contributor_id) is None:
        raise HTTPException(status_code=404, detail=f"no contributor {contributor_id}")
    events = await run_in_threadpool(store.reputation_events, contributor_id)
    return [
        ReputationEventOut(
            issue_id=e.issue_id,
            repo=e.repo,
            amount=money(e.amount),
            points=e.points,
            settled_at=e.settled_at,
        )
        for e in events
    ]


@router.get(
    "/{contributor_id}/statements/{year}",
    response_model=AnnualStatement,
    responses={200: {"content": {"text/csv": {}}}},
)
async def statement(
    contributor_id: str,
    year: int,
    format: Literal["json", "csv"] = Query(default="json"),
    account: Account | None = SIGNED_IN,
) -> AnnualStatement | Response:
    """One contributor's payouts for one calendar year, to file from.

    A statement is personal: its settlement references would link a wallet to a
    handle. It is served to the signed-in contributor it belongs to, and by the
    simulation, whose numbers are not real; an operator exports one with
    `python -m misthos.services.statements`.
    """
    require_owner_or_simulation(account, contributor_id, "see this statement")
    found = await run_in_threadpool(annual_statement, store, contributor_id, year)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no contributor {contributor_id}")
    if format == "csv":
        return Response(
            content=to_csv(found),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="misthos-{contributor_id}-{year}.csv"'
            },
        )
    return found

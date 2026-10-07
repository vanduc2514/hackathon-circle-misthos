from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool

from misthos.config import settings
from misthos.schemas import AnnualStatement, ContributorProfile
from misthos.services.statements import annual_statement, to_csv
from misthos.store import store

router = APIRouter(prefix="/contributors", tags=["contributors"])


@router.get("", response_model=list[ContributorProfile])
async def list_contributors() -> list[ContributorProfile]:
    """Public profiles. A wallet is never served next to a handle; see docs/PRIVACY.md."""
    contributors = await run_in_threadpool(store.list_contributors)
    return [ContributorProfile.of(c) for c in contributors]


@router.get(
    "/{contributor_id}/statements/{year}",
    response_model=AnnualStatement,
    responses={200: {"content": {"text/csv": {}}}},
)
async def statement(
    contributor_id: str,
    year: int,
    format: Literal["json", "csv"] = Query(default="json"),
) -> AnnualStatement | Response:
    """One contributor's payouts for one calendar year, to file from.

    A statement is personal: its settlement references would link a wallet to a
    handle. Until contributors can sign in it is served only by the simulation, whose
    numbers are not real; outside it, an operator exports it with
    `python -m misthos.services.statements`.
    """
    if not settings.simulated:
        raise HTTPException(
            status_code=403,
            detail="statements are served to the contributor once sign-in exists; "
            "export one with python -m misthos.services.statements",
        )
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

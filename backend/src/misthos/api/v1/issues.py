from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from misthos.config import settings
from misthos.domain.compliance import ComplianceRefusal
from misthos.domain.issue import IllegalTransition, IssueState
from misthos.domain.pricing import UnfundableIssue
from misthos.repositories import StaleIssue
from misthos.schemas import (
    Decision,
    HealthOut,
    IssueOut,
    IssueSummaryOut,
    MetricsOut,
    Publisher,
    PublishRequest,
    TimelineEntry,
)
from misthos.store import IssueRecord, store

router = APIRouter(tags=["issues"])

# The store may be talking to a database, so its calls run in the threadpool rather
# than blocking the event loop the sweeper and every other request share.


async def _require(issue_id: str) -> IssueRecord:
    rec = await run_in_threadpool(store.get, issue_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"no issue {issue_id}")
    return rec


def _conflict(exc: Exception) -> HTTPException:
    # A stale copy means someone, or the sweeper, moved the issue first. Retrying
    # reads the new state; overwriting it could undo a refund or a release.
    return HTTPException(status_code=409, detail=str(exc))


def _refused(exc: ComplianceRefusal) -> HTTPException:
    # Well formed and legal in the lifecycle, but money may not move for this party.
    return HTTPException(status_code=403, detail=str(exc))


@router.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    return HealthOut(
        status="ok",
        service=settings.app_name,
        chain=settings.chain,
        seeded_issues=await run_in_threadpool(store.count_issues),
        simulated=settings.simulated,
    )


@router.get("/issues", response_model=list[IssueSummaryOut])
async def list_issues(
    state: str | None = Query(default=None, description="Filter by lifecycle state"),
    publisher_id: str | None = None,
    compliance_only: bool = False,
) -> list[IssueSummaryOut]:
    states = None
    if state:
        wanted = state.upper()
        if wanted not in IssueState.__members__:
            raise HTTPException(status_code=400, detail=f"unknown state {state}")
        states = {IssueState(wanted)}
    records = await run_in_threadpool(store.list_issues, states)
    if publisher_id:
        records = [r for r in records if r.publisher_id == publisher_id]
    if compliance_only:
        records = [r for r in records if r.compliance_driven]
    records.sort(key=lambda r: r.created_at, reverse=True)
    return await run_in_threadpool(store.summaries, records)


@router.get("/issues/{issue_id}", response_model=IssueOut)
async def get_issue(issue_id: str) -> IssueOut:
    rec = await _require(issue_id)
    return await run_in_threadpool(store.to_out, rec)


@router.get("/issues/{issue_id}/timeline", response_model=list[TimelineEntry])
async def get_timeline(issue_id: str) -> list[TimelineEntry]:
    return store.timeline(await _require(issue_id))


@router.post("/issues/{issue_id}/advance", response_model=IssueOut)
async def advance(issue_id: str) -> IssueOut:
    """Move the issue one step along the demo path."""
    await _require(issue_id)
    try:
        rec = await run_in_threadpool(store.advance, issue_id)
    except (IllegalTransition, StaleIssue) as exc:
        raise _conflict(exc) from exc
    except ComplianceRefusal as exc:
        raise _refused(exc) from exc
    return await run_in_threadpool(store.to_out, rec)


@router.post("/issues/{issue_id}/complete", response_model=IssueOut)
async def complete(issue_id: str) -> IssueOut:
    """Run the rest of the happy path: verdict, merge, release."""
    await _require(issue_id)
    try:
        rec = await run_in_threadpool(store.approve_and_accept, issue_id)
    except (IllegalTransition, StaleIssue) as exc:
        raise _conflict(exc) from exc
    except ComplianceRefusal as exc:
        raise _refused(exc) from exc
    return await run_in_threadpool(store.to_out, rec)


@router.post("/issues", response_model=IssueOut, status_code=201)
async def publish(payload: PublishRequest) -> IssueOut:
    if await run_in_threadpool(store.get_publisher, payload.publisher_id) is None:
        raise HTTPException(status_code=400, detail=f"unknown publisher {payload.publisher_id}")
    try:
        rec = await run_in_threadpool(store.publish, payload)
    except UnfundableIssue as exc:
        # Well formed, but the work cannot carry its own review cost. Say why rather
        # than publish a price the platform loses money on.
        raise HTTPException(status_code=422, detail=exc.justification) from exc
    return await run_in_threadpool(store.to_out, rec)


@router.get("/publishers", response_model=list[Publisher])
async def list_publishers() -> list[Publisher]:
    return await run_in_threadpool(store.list_publishers)


@router.get("/metrics", response_model=MetricsOut)
async def metrics() -> MetricsOut:
    return await run_in_threadpool(store.metrics)


@router.get("/decisions", response_model=list[Decision])
async def decisions(limit: int = Query(default=50, le=500)) -> list[Decision]:
    """The append-only decision log across every issue, newest first."""
    return await run_in_threadpool(store.list_decisions, limit)


@router.post("/demo/reset")
async def reset() -> dict[str, int]:
    # Reset wipes every table when a database is configured. That is a simulation
    # tool, so it is refused anywhere the simulation is switched off.
    if not settings.simulated:
        raise HTTPException(status_code=403, detail="reset is only available in simulation")
    await run_in_threadpool(store.reset)
    return {"issues": await run_in_threadpool(store.count_issues)}

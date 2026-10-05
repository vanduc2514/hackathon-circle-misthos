from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from misthos.config import settings
from misthos.domain.issue import IllegalTransition, IssueState
from misthos.schemas import (
    Contributor,
    Decision,
    HealthOut,
    IssueOut,
    IssueSummaryOut,
    MetricsOut,
    Publisher,
    PublishRequest,
    Reviewer,
    TimelineEntry,
)
from misthos.store import store

router = APIRouter(tags=["issues"])


@router.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    return HealthOut(
        status="ok",
        service=settings.app_name,
        chain=settings.chain,
        seeded_issues=len(store.issues),
        simulated=settings.simulated,
    )


@router.get("/issues", response_model=list[IssueSummaryOut])
async def list_issues(
    state: str | None = Query(default=None, description="Filter by lifecycle state"),
    publisher_id: str | None = None,
    compliance_only: bool = False,
) -> list[IssueSummaryOut]:
    records = list(store.issues.values())
    if state:
        wanted = state.upper()
        if wanted not in IssueState.__members__:
            raise HTTPException(status_code=400, detail=f"unknown state {state}")
        records = [r for r in records if r.state.value == wanted]
    if publisher_id:
        records = [r for r in records if r.publisher_id == publisher_id]
    if compliance_only:
        records = [r for r in records if r.compliance_driven]
    records.sort(key=lambda r: r.created_at, reverse=True)
    return [store.summary(r) for r in records]


@router.get("/issues/{issue_id}", response_model=IssueOut)
async def get_issue(issue_id: str) -> IssueOut:
    rec = store.get(issue_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"no issue {issue_id}")
    return store.to_out(rec)


@router.get("/issues/{issue_id}/timeline", response_model=list[TimelineEntry])
async def get_timeline(issue_id: str) -> list[TimelineEntry]:
    rec = store.get(issue_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"no issue {issue_id}")
    return store.timeline(rec)


@router.post("/issues/{issue_id}/advance", response_model=IssueOut)
async def advance(issue_id: str) -> IssueOut:
    """Move the issue one step along the demo path."""
    if store.get(issue_id) is None:
        raise HTTPException(status_code=404, detail=f"no issue {issue_id}")
    try:
        rec = store.advance(issue_id)
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return store.to_out(rec)


@router.post("/issues/{issue_id}/complete", response_model=IssueOut)
async def complete(issue_id: str) -> IssueOut:
    """Run the rest of the happy path, including release and the review fee."""
    if store.get(issue_id) is None:
        raise HTTPException(status_code=404, detail=f"no issue {issue_id}")
    try:
        rec = store.approve_and_accept(issue_id)
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return store.to_out(rec)


@router.post("/issues", response_model=IssueOut, status_code=201)
async def publish(payload: PublishRequest) -> IssueOut:
    if payload.publisher_id not in store.publishers:
        raise HTTPException(status_code=400, detail=f"unknown publisher {payload.publisher_id}")
    rec = store.publish(payload)
    return store.to_out(rec)


@router.get("/publishers", response_model=list[Publisher])
async def list_publishers() -> list[Publisher]:
    return list(store.publishers.values())


@router.get("/contributors", response_model=list[Contributor])
async def list_contributors() -> list[Contributor]:
    return list(store.contributors.values())


@router.get("/reviewers", response_model=list[Reviewer])
async def list_reviewers() -> list[Reviewer]:
    return list(store.reviewers.values())


@router.get("/metrics", response_model=MetricsOut)
async def metrics() -> MetricsOut:
    return store.metrics()


@router.get("/decisions", response_model=list[Decision])
async def decisions(limit: int = Query(default=50, le=500)) -> list[Decision]:
    """The append-only decision log across every issue, newest first."""
    rows = [d for rec in store.issues.values() for d in rec.decisions]
    rows.sort(key=lambda d: d.created_at, reverse=True)
    return rows[:limit]


@router.post("/demo/reset")
async def reset() -> dict[str, int]:
    store.reset()
    return {"issues": len(store.issues)}

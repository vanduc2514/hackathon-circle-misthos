from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from misthos.api.guards import idempotent, limit_actions, limit_publish
from misthos.config import settings
from misthos.domain.compliance import ComplianceRefusal
from misthos.domain.issue import IllegalTransition, IssueState
from misthos.domain.policy import PolicyRefusal
from misthos.domain.pricing import UnfundableIssue
from misthos.repositories import StaleIssue
from misthos.schemas import (
    ApproveReleaseRequest,
    Decision,
    DeclineRequest,
    DisputeRequest,
    HealthOut,
    IssueOut,
    IssueSummaryOut,
    LoopOut,
    MetricsOut,
    Publisher,
    PublishRequest,
    TimelineEntry,
)
from misthos.services.chain import ChainRevert
from misthos.services.coordination import Busy
from misthos.services.github import GitHubError
from misthos.services.review import ReviewFailed
from misthos.store import (
    DeclineRefused,
    DisputeRefused,
    IssueRecord,
    UnreadableIssue,
    store,
)

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
    if isinstance(exc, Busy):
        # Someone holds the issue for an action that takes well under a second.
        return HTTPException(status_code=409, detail=str(exc), headers={"Retry-After": "1"})
    if isinstance(exc, ChainRevert):
        # The escrow refused the call, so no money moved and nothing was saved.
        return HTTPException(status_code=409, detail=f"the escrow refused: {exc}")
    return HTTPException(status_code=409, detail=str(exc))


# Sent with anything that moves money or creates work, so a retry after a timeout
# gets the first answer back instead of a second payment or a duplicate issue.
IDEMPOTENCY_KEY = Header(
    default=None,
    description="Any unique string, such as a UUID. Retries with the same key get "
    "the first response back for 24 hours instead of running again.",
)


def _refused(exc: Exception) -> HTTPException:
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


async def _act(step: Callable[..., IssueRecord], issue_id: str, *args: object) -> IssueOut:
    try:
        rec = await run_in_threadpool(step, issue_id, *args)
    except (
        IllegalTransition,
        StaleIssue,
        Busy,
        ChainRevert,
        DisputeRefused,
        DeclineRefused,
    ) as exc:
        raise _conflict(exc) from exc
    except (ComplianceRefusal, PolicyRefusal) as exc:
        raise _refused(exc) from exc
    except (ReviewFailed, GitHubError) as exc:
        # Something we depend on failed; nothing was saved, so the step can be retried.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return await run_in_threadpool(store.to_out, rec)


@router.post(
    "/issues/{issue_id}/advance",
    response_model=IssueOut,
    dependencies=[limit_actions],
    responses={409: {"description": "Illegal step, busy issue or refused by the escrow"}},
)
async def advance(
    issue_id: str, request: Request, idempotency_key: str | None = IDEMPOTENCY_KEY
) -> Any:
    """Move the issue one step along the demo path."""
    await _require(issue_id)
    return await idempotent(request, idempotency_key, lambda: _act(store.advance, issue_id))


@router.post(
    "/issues/{issue_id}/complete",
    response_model=IssueOut,
    dependencies=[limit_actions],
    responses={409: {"description": "Illegal step, busy issue or refused by the escrow"}},
)
async def complete(
    issue_id: str, request: Request, idempotency_key: str | None = IDEMPOTENCY_KEY
) -> Any:
    """Run the rest of the happy path: verdict, merge, release."""
    await _require(issue_id)
    return await idempotent(
        request, idempotency_key, lambda: _act(store.approve_and_accept, issue_id)
    )


@router.post(
    "/issues/{issue_id}/decline",
    response_model=IssueOut,
    dependencies=[limit_actions],
    responses={409: {"description": "Not awaiting the merge, or already declined once"}},
)
async def decline(
    issue_id: str,
    payload: DeclineRequest,
    request: Request,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
) -> Any:
    """The publisher declines work the review passed, with a reason. Once per issue:
    the work goes back for rework, and after that the merge or the grace period
    settles it."""
    # Only the publisher may decline, and publishers cannot sign in yet (#70).
    if not settings.simulated:
        raise HTTPException(status_code=403, detail="declining needs publisher sign-in")
    await _require(issue_id)
    return await idempotent(
        request, idempotency_key, lambda: _act(store.decline, issue_id, payload.reason)
    )


@router.post(
    "/issues/{issue_id}/approve-release",
    response_model=IssueOut,
    dependencies=[limit_actions],
    responses={
        403: {"description": "Not one of the organisation's named approvers"},
        409: {"description": "No release is waiting for approval"},
    },
)
async def approve_release(
    issue_id: str,
    payload: ApproveReleaseRequest,
    request: Request,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
) -> Any:
    """A named approver approves a release held over the organisation's threshold."""
    if not settings.simulated:
        raise HTTPException(status_code=403, detail="approving needs publisher sign-in")
    await _require(issue_id)
    return await idempotent(
        request, idempotency_key, lambda: _act(store.approve_release, issue_id, payload.approver)
    )


@router.get("/loop", response_model=LoopOut)
async def loop(
    repo: str | None = Query(default=None, description="owner/name; every repository if empty"),
) -> LoopOut:
    """The loop in public: issues funded, settled and paid, for one repository or all.
    Built for a repository's watchers to see, so it carries no wallet or transfer."""
    return await run_in_threadpool(store.loop, repo)


@router.post(
    "/issues/{issue_id}/dispute",
    response_model=IssueOut,
    dependencies=[limit_actions],
    responses={409: {"description": "Nothing to dispute, or already disputed"}},
)
async def dispute(
    issue_id: str,
    payload: DisputeRequest,
    request: Request,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
) -> Any:
    """The contributor challenges a rework or reject verdict: the same commit is
    reviewed again against the published criteria, and the outcome is recorded."""
    # Only the contributor may dispute, and contributors cannot sign in yet (#70), so
    # the endpoint is the simulation's until they can.
    if not settings.simulated:
        raise HTTPException(status_code=403, detail="disputes need contributor sign-in")
    await _require(issue_id)
    return await idempotent(
        request, idempotency_key, lambda: _act(store.dispute, issue_id, payload.reason)
    )


@router.post(
    "/issues",
    response_model=IssueOut,
    status_code=201,
    dependencies=[limit_publish],
)
async def publish(
    payload: PublishRequest, request: Request, idempotency_key: str | None = IDEMPOTENCY_KEY
) -> Any:
    if await run_in_threadpool(store.get_publisher, payload.publisher_id) is None:
        raise HTTPException(status_code=400, detail=f"unknown publisher {payload.publisher_id}")

    async def run() -> IssueOut:
        try:
            rec = await run_in_threadpool(store.publish, payload)
        except UnfundableIssue as exc:
            # Well formed, but the work cannot carry its own review cost. Say why
            # rather than publish a price the platform loses money on.
            raise HTTPException(status_code=422, detail=exc.justification) from exc
        except UnreadableIssue as exc:
            # The App is not installed there, or the issue does not exist.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await run_in_threadpool(store.to_out, rec)

    return await idempotent(request, idempotency_key, run, status_code=201)


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

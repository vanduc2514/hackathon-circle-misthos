from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from misthos.api.guards import idempotent, limit_actions, limit_publish
from misthos.api.public import public
from misthos.api.session import (
    SIGNED_IN,
    require_github,
    require_owner,
    require_role,
)
from misthos.api.v1.auth import callback_url, circle_sign_in_configured, oauth_configured
from misthos.config import settings
from misthos.domain.compliance import ComplianceRefusal
from misthos.domain.escrow import approve_call, commit_call, issue_key
from misthos.domain.issue import IllegalTransition, IssueState
from misthos.domain.money import Usdc
from misthos.domain.policy import PolicyRefusal
from misthos.domain.pricing import PriceRefused
from misthos.repositories import StaleIssue
from misthos.schemas import (
    Account,
    AlreadyPublished,
    ApproveReleaseRequest,
    ClaimRequest,
    CommitmentPlan,
    CriteriaRequest,
    Decision,
    DeclineRequest,
    DisputeRequest,
    EscrowReadback,
    FundRequest,
    HealthOut,
    IssueOut,
    IssueSummaryOut,
    MetricsOut,
    PriceRequest,
    Publisher,
    PublisherListing,
    PublishRequest,
    SubmitRequest,
    TimelineEntry,
    WalletCall,
    money,
)
from misthos.services.chain import ChainGateway, ChainRevert, ChainUnavailable
from misthos.services.coordination import Busy
from misthos.services.github import GitHubError
from misthos.services.review import ReviewFailed
from misthos.store import (
    ESCROW,
    AlreadyListed,
    CriteriaNotApproved,
    DeclineRefused,
    DisputeRefused,
    IssueRecord,
    NotTheSubmission,
    UnreadableIssue,
    UntestableCriteria,
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


def _who(account: Account | None, fallback: str) -> str:
    return (account.github_login or account.address) if account else fallback


def _refused(exc: Exception) -> HTTPException:
    # Well formed and legal in the lifecycle, but money may not move for this party.
    return HTTPException(status_code=403, detail=str(exc))


@router.get(
    "/health",
    response_model=HealthOut,
    openapi_extra=public(
        "Core",
        "Health",
        "Network, contract addresses and the money mode this deployment is running.",
    ),
)
async def health() -> HealthOut:
    return HealthOut(
        status="ok",
        service=settings.app_name,
        chain=settings.chain,
        chain_id=settings.chain_id,
        network_label=settings.network.label,
        money=settings.network.money,
        money_note=settings.network.description,
        # The web app offers "Link with GitHub" only where it can work (#118).
        github_oauth=oauth_configured(),
        github_oauth_callback_url=callback_url(),
        circle_sign_in=circle_sign_in_configured(),
    )


@router.get(
    "/issues",
    response_model=list[IssueSummaryOut],
    openapi_extra=public(
        "Marketplace",
        "List issues",
        "Every published issue with its price, state and acceptance criteria.",
    ),
)
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


@router.get(
    "/issues/{issue_id}",
    response_model=IssueOut,
    openapi_extra=public(
        "Marketplace",
        "Issue detail",
        "One issue, its escrow commitment, publisher and current lifecycle state.",
    ),
)
async def get_issue(issue_id: str) -> IssueOut:
    rec = await _require(issue_id)
    return await run_in_threadpool(store.to_out, rec)


@router.get(
    "/issues/{issue_id}/timeline",
    response_model=list[TimelineEntry],
    openapi_extra=public(
        "Marketplace",
        "Issue timeline",
        "The public audit trail: pricing, funding, claims, reviews and settlement.",
    ),
)
async def get_timeline(issue_id: str) -> list[TimelineEntry]:
    return store.timeline(await _require(issue_id))


def _escrow_source() -> tuple[ChainGateway, str]:
    """The escrow the store settles through."""
    return store.chain, "chain"


@router.get("/issues/{issue_id}/commitment", response_model=CommitmentPlan)
async def commitment_plan(
    issue_id: str, account: Account | None = SIGNED_IN
) -> CommitmentPlan:
    """The two transactions the publisher's own wallet sends to fund the issue.

    The platform never signs a commitment: it moves the publisher's USDC. Approving the
    price records it as the escrow's ceiling; the publisher's wallet then lets the escrow
    take that amount and commits it, and approving again books it.

    The amount is the publisher's price -- the same value `_fund` records as the ceiling,
    and never the engine's suggestion. The escrow refuses any commitment above the
    ceiling (`ExceedsCeiling`), so a plan built from the suggestion could not be booked:
    the wallet would be asked for a number the contract had already ruled out (#150).

    The deadline is the one the approval fixed, to the second, and the amount and rate
    are the approved ones too. Working them out here instead is what made the booking
    refuse a commitment once an hour had passed between the plan and the booking: the
    escrow's term starts at the commit, so `now + 14d` no longer matches what is on
    chain, and it never matches again (#126).
    """
    rec = await _require(issue_id)
    require_owner(account, rec.publisher_id, "fund this issue")
    terms = rec.funding
    if rec.price is None or terms is None:
        raise HTTPException(
            status_code=409,
            detail=f"{issue_id} has no approved price yet, so approve it before committing",
        )
    amount = terms.amount
    deadline = int(terms.deadline.timestamp())
    return CommitmentPlan(
        issue_id=issue_id,
        chain_id=ESCROW.chain_id,
        escrow=ESCROW.address,
        usdc=settings.usdc_address,
        amount=money(amount),
        deadline=deadline,
        calls=[
            WalletCall(
                label=f"Let the escrow take {amount}",
                to=settings.usdc_address,
                data=approve_call(ESCROW.address, amount),
            ),
            WalletCall(
                label=f"Commit {amount} to {issue_id}",
                to=ESCROW.address,
                data=commit_call(issue_id, amount, deadline),
            ),
        ],
    )


@router.get(
    "/issues/{issue_id}/escrow",
    response_model=EscrowReadback,
    openapi_extra=public(
        "Trust",
        "Escrow readback",
        "The commitment as MisthosEscrow on Arc holds it, read from the chain.",
    ),
)
async def get_escrow(issue_id: str) -> EscrowReadback:
    """Read the commitment back from the escrow, so nobody has to take our word."""
    await _require(issue_id)
    gateway, source = _escrow_source()
    try:
        held = await run_in_threadpool(gateway.commitment, issue_id)
        ceiling = await run_in_threadpool(gateway.escrow_ceiling, issue_id)
    except ChainUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return EscrowReadback(
        issue_id=issue_id,
        issue_key=issue_key(issue_id),
        contract=ESCROW.address,
        chain_id=ESCROW.chain_id,
        source=source,  # type: ignore[arg-type]
        status=held.status.value if held else "none",  # type: ignore[arg-type]
        publisher=held.publisher if held else None,
        amount=money(held.amount if held else Usdc(0)),
        escrow_ceiling=money(ceiling) if ceiling else None,
        deadline=held.deadline if held else None,
        explorer_url=f"{settings.explorer_url}/address/{ESCROW.address}",
    )


async def _act(
    step: Callable[..., IssueRecord], issue_id: str, *args: object, **kwargs: object
) -> IssueOut:
    try:
        rec = await run_in_threadpool(step, issue_id, *args, **kwargs)
    except (
        IllegalTransition,
        StaleIssue,
        Busy,
        ChainRevert,
        DisputeRefused,
        DeclineRefused,
        CriteriaNotApproved,
        NotTheSubmission,
    ) as exc:
        raise _conflict(exc) from exc
    except (ComplianceRefusal, PolicyRefusal) as exc:
        raise _refused(exc) from exc
    except PriceRefused as exc:
        # The request named a price; it is the value in it that cannot be used, which is
        # what 422 says, rather than the party being refused.
        raise HTTPException(status_code=422, detail=exc.justification) from exc
    except UntestableCriteria as exc:
        # Shaped like a validation error, so a client shows each reason by its criterion.
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["body", "criteria", p.index - 1],
                    "msg": f"Criterion {p.index} {p.reason}.",
                    "type": "untestable_criterion",
                }
                for p in exc.problems
            ],
        ) from exc
    except (ReviewFailed, GitHubError) as exc:
        # Something we depend on failed; nothing was saved, so the step can be retried.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ChainUnavailable as exc:
        # The chain could not be read, or a call was refused for being too fast. Nothing
        # was saved, so the step can be retried; 503 with a pause, not a 500 without one,
        # because "come back" is actionable and "server error" is not (#154).
        raise HTTPException(
            status_code=503, detail=str(exc), headers={"Retry-After": "2"}
        ) from exc
    return await run_in_threadpool(store.to_out, rec)


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
    account: Account | None = SIGNED_IN,
) -> Any:
    """The publisher declines work the review passed, with a reason. Once per issue:
    the work goes back for rework, and after that the merge or the grace period
    settles it."""
    rec = await _require(issue_id)
    require_owner(account, rec.publisher_id, "decline this work")
    return await idempotent(
        request, idempotency_key, lambda: _act(store.decline, issue_id, payload.reason)
    )


@router.post(
    "/issues/{issue_id}/approve-release",
    response_model=IssueOut,
    dependencies=[limit_actions],
    responses={
        403: {
            "description": "Not signed in with a GitHub login the organisation named, "
            "or the contributor being paid"
        },
        409: {"description": "No release is waiting for approval"},
    },
)
async def approve_release(
    issue_id: str,
    request: Request,
    payload: ApproveReleaseRequest | None = None,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    account: Account | None = SIGNED_IN,
) -> Any:
    """A named approver approves a release held over the organisation's threshold.

    The approver is whoever is signed in, by the GitHub login linked to their account,
    and the organisation must have named that login. A name in the request would let
    anyone who knew an approver's name release the money, so it counts only in the
    the signed-in account, which must carry a linked GitHub login.
    """
    await _require(issue_id)
    if account is not None:
        require_github(account, "approve a release")
        approver = account.github_login
    else:
        raise HTTPException(status_code=401, detail="sign in as a named approver to approve")
    return await idempotent(
        request, idempotency_key, lambda: _act(store.approve_release, issue_id, approver)
    )


# ---------------------------------------------------------- explicit actions (#71)


@router.post("/issues/{issue_id}/criteria", response_model=IssueOut, dependencies=[limit_actions])
async def approve_criteria(
    issue_id: str,
    payload: CriteriaRequest,
    request: Request,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    account: Account | None = SIGNED_IN,
) -> Any:
    """The publisher edits the drafted acceptance criteria and approves them. No
    issue is funded without approved criteria (#21)."""
    rec = await _require(issue_id)
    require_owner(account, rec.publisher_id, "approve these criteria")
    if account is not None:
        require_github(account, "approve criteria")
    by = _who(account, "the publisher")
    return await idempotent(
        request,
        idempotency_key,
        lambda: _act(store.approve_criteria, issue_id, payload.criteria, by),
    )


@router.post("/issues/{issue_id}/fund", response_model=IssueOut, dependencies=[limit_actions])
async def fund(
    issue_id: str,
    request: Request,
    payload: FundRequest | None = None,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    account: Account | None = SIGNED_IN,
) -> Any:
    """The publisher approves the price they set, and the money is committed.

    No amount is taken here. The price was named when the issue was published and can
    be moved with `PATCH /issues/{issue_id}/price` until someone claims it, so a second
    way to change the number at the moment money moves is a second thing to audit (#142).
    """
    rec = await _require(issue_id)
    require_owner(account, rec.publisher_id, "fund this issue")
    if account is not None:
        require_github(account, "fund an issue")
    by = _who(account, "the publisher")
    # Fix the terms first, in a save of their own: the booking may be refused with
    # `NotCommitted`, which discards the save it happens in, and the plan and the booking
    # both read the terms back (#126).
    await run_in_threadpool(store.record_terms, issue_id)
    return await idempotent(
        request,
        idempotency_key,
        lambda: _act(store.approve_price, issue_id, by),
    )


@router.patch(
    "/issues/{issue_id}/price",
    response_model=IssueOut,
    dependencies=[limit_actions],
    responses={409: {"description": "An issue past the point where its price can move"}},
)
async def set_price(
    issue_id: str,
    payload: PriceRequest,
    request: Request,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    account: Account | None = SIGNED_IN,
) -> Any:
    """The publisher names a new price, while nobody is working on the issue.

    Until a claim the price is the publisher's to move: the money is theirs and the
    issue has not been promised to anyone. A claim is that promise, so it closes the
    door. This is what replaces the automatic re-listing that used to raise a price
    nobody claimed on the publisher's behalf (#142).
    """
    rec = await _require(issue_id)
    require_owner(account, rec.publisher_id, "re-price this issue")
    if account is not None:
        require_github(account, "re-price an issue")
    by = _who(account, "the publisher")
    amount = _amount(payload.amount)
    return await idempotent(
        request,
        idempotency_key,
        lambda: _act(store.set_price, issue_id, amount, by),
    )


def _amount(text: str) -> Usdc:
    """A price the publisher typed, as USDC.

    Parsing only. The bounds are the domain's to enforce, so a refusal and the range
    that would work come from one place (`domain/pricing.check_price`).
    """
    try:
        return Usdc.from_decimal(text.strip().replace(",", ""))
    except (ArithmeticError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{text!r} is not an amount in USDC") from exc


@router.post("/issues/{issue_id}/claim", response_model=IssueOut, dependencies=[limit_actions])
async def claim(
    issue_id: str,
    request: Request,
    payload: ClaimRequest | None = None,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    account: Account | None = SIGNED_IN,
) -> Any:
    """A contributor takes the exclusive, time-boxed claim. First claim wins."""
    await _require(issue_id)
    if account is not None:
        require_role(account, "contributor", "claim an issue")
        require_github(account, "claim an issue")
        contributor_id = account.party_id
    else:
        raise HTTPException(status_code=401, detail="sign in as a contributor to claim")
    if await run_in_threadpool(store.get_contributor, contributor_id) is None:
        raise HTTPException(status_code=400, detail=f"no contributor {contributor_id}")
    return await idempotent(
        request, idempotency_key, lambda: _act(store.claim, issue_id, contributor_id)
    )


@router.post("/issues/{issue_id}/submit", response_model=IssueOut, dependencies=[limit_actions])
async def submit(
    issue_id: str,
    payload: SubmitRequest,
    request: Request,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    account: Account | None = SIGNED_IN,
) -> Any:
    """The claimant submits their pull request for review. GitHub says who opened it,
    and only the claimant's own pull request counts."""
    rec = await _require(issue_id)
    require_owner(account, rec.contributor_id or "", "submit for this claim")
    if account is not None:
        require_github(account, "submit work")
    try:
        pr = await run_in_threadpool(store.github.read_pull_request, rec.repo, payload.pr_number)
    except GitHubError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if account is not None and pr.author.lower() != (account.github_login or "").lower():
        raise HTTPException(
            status_code=403, detail=f"#{pr.number} was opened by {pr.author}, not by you"
        )
    return await idempotent(
        request, idempotency_key, lambda: _act(store.submit_pull_request, issue_id, pr)
    )


@router.post("/issues/{issue_id}/review", response_model=IssueOut, dependencies=[limit_actions])
async def review(issue_id: str, account: Account | None = SIGNED_IN) -> IssueOut:
    """Have the review agent judge the submitted commit now rather than on the
    sweeper's next pass. The publisher or the claimant may ask."""
    rec = await _require(issue_id)
    if account is not None and account.party_id not in {rec.publisher_id, rec.contributor_id}:
        raise HTTPException(status_code=403, detail="only the publisher or the claimant can ask")
    if account is None:
        raise HTTPException(status_code=401, detail="sign in to ask for a review")
    try:
        reviewed = await run_in_threadpool(store.review, issue_id)
    except (ReviewFailed, GitHubError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (Busy, StaleIssue) as exc:
        raise _conflict(exc) from exc
    if reviewed is None:
        raise HTTPException(status_code=409, detail=f"{issue_id} has no commit awaiting a verdict")
    return await run_in_threadpool(store.to_out, reviewed)


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
    account: Account | None = SIGNED_IN,
) -> Any:
    """The contributor challenges a rework or reject verdict: the same commit is
    reviewed again against the published criteria, and the outcome is recorded."""
    rec = await _require(issue_id)
    require_owner(account, rec.contributor_id or "", "dispute this verdict")
    return await idempotent(
        request, idempotency_key, lambda: _act(store.dispute, issue_id, payload.reason)
    )


async def already_published(_: Request, exc: Exception) -> JSONResponse:
    """A GitHub issue already listed and open: 409, naming the listing, so the web app
    can link to it. Registered on the app, because the refusal comes from the store
    inside the idempotency guard."""
    assert isinstance(exc, AlreadyListed)
    body = AlreadyPublished(detail=str(exc), issue_id=exc.issue_id)
    return JSONResponse(status_code=409, content=body.model_dump())


@router.post(
    "/issues",
    response_model=IssueOut,
    status_code=201,
    dependencies=[limit_publish],
    responses={
        409: {
            "model": AlreadyPublished,
            "description": "The GitHub issue already has an open listing, which "
            "`issue_id` names",
        }
    },
)
async def publish(
    payload: PublishRequest,
    request: Request,
    idempotency_key: str | None = IDEMPOTENCY_KEY,
    account: Account | None = SIGNED_IN,
) -> Any:
    if account is not None:
        require_role(account, "publisher", "publish an issue")
        require_github(account, "publish an issue")
        # A signed-in publisher publishes as itself, whatever the form says.
        payload = payload.model_copy(update={"publisher_id": account.party_id})
    else:
        raise HTTPException(status_code=401, detail="sign in to publish an issue")
    if await run_in_threadpool(store.get_publisher, payload.publisher_id) is None:
        raise HTTPException(status_code=400, detail=f"unknown publisher {payload.publisher_id}")
    if payload.price is None:
        # The web form always sends one. The GitHub flow lists an issue unpriced on
        # purpose and names the price later with `/misthos price` (#142).
        raise HTTPException(
            status_code=422, detail="name a price: the publisher sets what the work is worth"
        )

    async def run() -> IssueOut:
        try:
            rec = await run_in_threadpool(store.publish, payload)
        except PriceRefused as exc:
            # Well formed, but outside the bounds a price may be published at. This is
            # the only thing that refuses a price (#142).
            raise HTTPException(status_code=422, detail=exc.justification) from exc
        except UnreadableIssue as exc:
            # The App is not installed there, or the issue does not exist.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Busy as exc:
            # Another publish of the same GitHub issue held it past the wait.
            raise _conflict(exc) from exc
        return await run_in_threadpool(store.to_out, rec)

    return await idempotent(request, idempotency_key, run, status_code=201)


@router.get(
    "/publishers",
    response_model=list[PublisherListing],
    openapi_extra=public(
        "Marketplace",
        "Publisher directory",
        "Every publisher and its wallet; its budget and policy only to itself.",
    ),
)
async def list_publishers(account: Account | None = SIGNED_IN) -> list[PublisherListing]:
    """Every publisher. A publisher's budget and spending policy are served only to
    that publisher, signed in."""
    rows = await run_in_threadpool(store.list_publishers)
    mine = account.party_id if account is not None else None
    return [_listing(p, own=p.id == mine) for p in rows]


def _listing(publisher: Publisher, *, own: bool) -> PublisherListing:
    if own:
        return PublisherListing(**publisher.model_dump())
    return PublisherListing(
        id=publisher.id,
        name=publisher.name,
        kind=publisher.kind,
        wallet=publisher.wallet,
    )


@router.get(
    "/metrics",
    response_model=MetricsOut,
    openapi_extra=public(
        "Analytics",
        "Marketplace metrics",
        "Settlement volume, take rate, time to merge and live issue counts.",
    ),
)
async def metrics() -> MetricsOut:
    return await run_in_threadpool(store.metrics)


@router.get(
    "/decisions",
    response_model=list[Decision],
    openapi_extra=public(
        "Trust",
        "Decision log",
        "The newest decisions, up to 500: who decided what, by which rule.",
    ),
)
async def decisions(limit: int = Query(default=50, le=500)) -> list[Decision]:
    """The append-only decision log across every issue, newest first."""
    return await run_in_threadpool(store.list_decisions, limit)

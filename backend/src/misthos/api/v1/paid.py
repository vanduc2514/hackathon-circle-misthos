"""The core's side of the paid API: only the edge may call it (#200).

An agent buys a call from the edge over x402. The edge verifies the payment, asks the
core here to do the work, and settles only if the core answered. These routes are
therefore internal: every request must carry the edge's `X-Misthos-Core-Token`, and one
that does not is answered 404, as if nothing were here. They are not in the API schema.

Nothing here moves money, changes an issue, or touches a session, wallet, statement or
policy. The only writes are the paid API's own books: settlements, and the quotes it
gave. The edge's catalog is the allowlist of what is sold, and
each entry names one route under this prefix.

- `POST /access` says whether a signed-in caller may use a paid route free.
- `POST /calls` writes down a settlement once the edge has settled it.
- `GET /calls` reads the books of paid calls back, to reconcile with Circle Gateway.
- `GET /signer` names the address certificates are signed by.

The views an agent buys are below those: the decision log, a reputation, an escrow
attestation (#202); a price and its comparables (#203); a pre-flight review (#204);
opportunities, a settlement receipt and a sanctions screen (#206).
Each answers an error the edge does not charge for when it cannot answer: a missing
record, an unknown filter, a chain it cannot read.
"""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from misthos.auth import sessions
from misthos.config import settings
from misthos.domain.money import Usdc
from misthos.domain.paid import PaidCall, totals
from misthos.schemas import (
    PaidAccess,
    PaidAccessAsked,
    PaidCallIn,
    PaidCallOut,
    PaidCallsOut,
    PaidRouteTotal,
    money,
)
from misthos.services import certificates, paid_deals, paid_pricing, paid_review, paid_views
from misthos.services.paid import PaidCaller, is_free, who
from misthos.services.paid_views import Unanswerable
from misthos.store import store

PREFIX = "/internal/paid"


async def edge_only(request: Request) -> None:
    """Refuse anyone but the edge, and say nothing about why."""
    expected = settings.edge_core_token
    given = request.headers.get("x-misthos-core-token", "")
    if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=404, detail="Not Found")


def paid_caller(request: Request) -> PaidCaller:
    """Who the edge says this call is for. Trusted because only the edge gets here."""
    return PaidCaller(
        route=request.headers.get("x-misthos-paid-route", ""),
        payer=request.headers.get("x-misthos-payer") or None,
        free_caller=request.headers.get("x-misthos-free-caller") or None,
    )


PAID_CALLER = Depends(paid_caller)

router = APIRouter(prefix=PREFIX, tags=["paid"], dependencies=[Depends(edge_only)])


@router.post("/access", response_model=PaidAccess)
async def access(asked: PaidAccessAsked, request: Request) -> PaidAccess:
    """Whether the caller the edge is holding may use `route` free.

    The edge passes on the caller's own `Authorization` in
    `X-Misthos-Caller-Authorization`; a session it names is looked up here and checked
    against the route's free-tier rule. Anything else is not free.
    """
    header = request.headers.get("x-misthos-caller-authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    address = sessions.address_in(token) if token else None
    account = await run_in_threadpool(store.repo.get_account, address) if address else None
    if account is None or not await run_in_threadpool(is_free, asked.route, account, asked.params):
        return PaidAccess(free=False)
    return PaidAccess(free=True, who=who(account))


def _call_out(call: PaidCall) -> PaidCallOut:
    return PaidCallOut(
        id=call.id,
        route=call.route,
        request_digest=call.request_digest,
        payer=call.payer,
        amount=money(call.amount),
        network=call.network,
        asset=call.asset,
        transaction=call.transaction,
        ref=call.ref,
        settled_at=call.settled_at,
    )


@router.post("/calls", response_model=PaidCallOut, status_code=201)
async def record_call(body: PaidCallIn, response: Response) -> PaidCallOut:
    """Write down a settlement. The same settlement twice is recorded once (200)."""
    call, new = await run_in_threadpool(
        lambda: store.record_paid_call(
            route=body.route,
            request_digest=body.request_digest,
            payer=body.payer,
            amount=Usdc(body.amount_base_units),
            network=body.network,
            asset=body.asset,
            transaction=body.transaction,
            ref=body.ref,
        )
    )
    if not new:
        response.status_code = 200
    return _call_out(call)


@router.get("/calls", response_model=PaidCallsOut)
async def list_calls(
    since: Annotated[datetime | None, Query()] = None,
    route: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> PaidCallsOut:
    """The books: settled calls, newest first, and what each route earned per network."""
    calls = await run_in_threadpool(
        lambda: store.list_paid_calls(since=since, route=route, limit=limit)
    )
    return PaidCallsOut(
        calls=[_call_out(c) for c in calls],
        totals=[
            PaidRouteTotal(route=t.route, network=t.network, calls=t.calls, amount=money(t.amount))
            for t in totals(calls)
        ],
        total=money(Usdc(sum(c.amount.base_units for c in calls))),
    )


# ----------------------------------------------------------- what agents buy


def refused(exc: Unanswerable) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": exc.error, "detail": exc.detail})


def only(request: Request, *allowed: str) -> None:
    """Refuse a filter the route does not know, rather than ignore it and charge for an
    answer the buyer did not ask for."""
    unknown = sorted(set(request.query_params) - set(allowed))
    if unknown:
        raise Unanswerable(
            400,
            "unknown_filter",
            f"unknown {', '.join(unknown)}; this route takes {', '.join(allowed) or 'none'}",
        )


@router.get("/signer")
async def signer() -> JSONResponse:
    """The address every certificate is signed by. Free: it is how one is checked."""
    try:
        address = await run_in_threadpool(certificates.signer)
    except certificates.CertificatesUnavailable:
        return refused(Unanswerable(503, "certificates_unavailable", "no certificate key"))
    return JSONResponse({"signer": address, "scheme": certificates.SCHEME})


@router.get("/decisions")
async def decisions(
    request: Request,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query()] = paid_views.DECISIONS_PAGE_DEFAULT,
    issue_id: Annotated[str | None, Query(max_length=32)] = None,
    actor: Annotated[str | None, Query(max_length=16)] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
) -> JSONResponse:
    """The whole decision log, a page at a time, oldest first (#202)."""
    try:
        only(request, "cursor", "limit", "issue_id", "actor", "action", "since", "until")
        page = await run_in_threadpool(
            lambda: paid_views.decisions_page(
                store,
                cursor=cursor,
                limit=limit,
                issue_id=issue_id,
                actor=actor,
                action=action,
                since=since,
                until=until,
            )
        )
    except Unanswerable as exc:
        return refused(exc)
    return JSONResponse(page)


@router.get("/reputation/{contributor_id}")
async def contributor_reputation(request: Request, contributor_id: str) -> JSONResponse:
    """A contributor's standing taken apart: score, signals, specialisation (#202)."""
    try:
        only(request)
        found = await run_in_threadpool(paid_views.reputation_of, store, contributor_id)
    except Unanswerable as exc:
        return refused(exc)
    return JSONResponse(found)


@router.get("/escrow/{issue_id}/attestation")
async def escrow_attestation(request: Request, issue_id: str) -> JSONResponse:
    """A signed certificate of what the escrow holds for an issue (#202)."""
    try:
        only(request)
        found = await run_in_threadpool(paid_views.escrow_attestation, store, issue_id)
    except Unanswerable as exc:
        return refused(exc)
    return JSONResponse(found)


@router.post("/price")
async def price(request: Request, caller: PaidCaller = PAID_CALLER) -> JSONResponse:
    """What the engine would suggest for an issue the caller describes (#203)."""
    try:
        only(request)
        try:
            body = await request.json()
        except ValueError as exc:
            raise Unanswerable(400, "bad_request", "the body is not JSON") from exc
        asked = paid_pricing.parse_price_asked(body)
        digest = request.headers.get("x-misthos-request-digest") or None
        report = await run_in_threadpool(paid_pricing.price, store, asked, caller, digest)
    except Unanswerable as exc:
        return refused(exc)
    return JSONResponse(report)


@router.get("/comparables")
async def comparables(request: Request) -> JSONResponse:
    """The settled history the engine compares against, filtered, a page at a time."""
    try:
        query = dict(request.query_params)
        page = await run_in_threadpool(paid_pricing.comparables_page, store, query)
    except Unanswerable as exc:
        return refused(exc)
    return JSONResponse(page)


@router.post("/review")
async def review(request: Request, caller: PaidCaller = PAID_CALLER) -> JSONResponse:
    """Queue a pre-flight review of a diff against acceptance criteria (#204). 202 and
    a job id; the edge polls the job and settles when it collects the result."""
    try:
        only(request)
        try:
            body = await request.json()
        except ValueError as exc:
            raise Unanswerable(400, "bad_request", "the body is not JSON") from exc
        asked = paid_review.parse_review_asked(body)
        digest = request.headers.get("x-misthos-request-digest") or None
        job = await run_in_threadpool(paid_review.submit, store, asked, caller, digest)
    except Unanswerable as exc:
        return refused(exc)
    return JSONResponse(
        status_code=202,
        content={"job_id": job.id, "status": job.status, "files": job.files},
    )


@router.get("/review/jobs/{job_id}")
async def review_job(job_id: str) -> JSONResponse:
    """Where a paid review is: queued, running, done with its result, or failed."""
    try:
        found = await run_in_threadpool(paid_review.poll, store, job_id)
    except Unanswerable as exc:
        return refused(exc)
    return JSONResponse(found)


@router.get("/opportunities")
async def opportunities(request: Request) -> JSONResponse:
    """Funded work still open, filtered and paged (#206)."""
    try:
        query = dict(request.query_params)
        page = await run_in_threadpool(paid_deals.opportunities_page, store, query)
    except Unanswerable as exc:
        return refused(exc)
    return JSONResponse(page)


@router.get("/receipt/{settlement_id}")
async def receipt(
    request: Request, settlement_id: str, caller: PaidCaller = PAID_CALLER
) -> JSONResponse:
    """A signed receipt of one issue's settlement, checked against the books (#206)."""
    try:
        only(request)
        found = await run_in_threadpool(paid_deals.receipt, store, settlement_id, caller)
    except Unanswerable as exc:
        return refused(exc)
    return JSONResponse(found)


@router.post("/screen")
async def screen(request: Request) -> JSONResponse:
    """A sanctions check of one wallet. Off by default: 503 until it is cleared (#206)."""
    try:
        only(request)
        try:
            body = await request.json()
        except ValueError:
            body = None
        found = await run_in_threadpool(paid_deals.screen, store, body)
    except Unanswerable as exc:
        return refused(exc)
    return JSONResponse(found)

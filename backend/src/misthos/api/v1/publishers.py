"""An organisation's own controls and records: its spending policy, its spend, its
audit export (epic #13), its single sign-on (#53), and its money as the pricing engine
sees it (#43).

These are the organisation's alone: a signed-in publisher sees and sets its own,
and the simulation lets the demo see any. An operator exports the audit record with
`python -m misthos.services.audit`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool

from misthos.api.session import SESSION, SIGNED_IN, require_owner_or_simulation
from misthos.api.v1.auth import oidc_client, sso_callback_url
from misthos.auth.sessions import Identity
from misthos.config import settings
from misthos.domain import plans
from misthos.domain import sso as single_sign_on
from misthos.domain.policy import PolicyRefusal
from misthos.repositories import SsoDomainTaken
from misthos.schemas import (
    Account,
    AuditExport,
    FinanceOut,
    PolicyRequest,
    Publisher,
    RepositoriesOut,
    SpendOut,
    SsoConnection,
    SsoOut,
    SsoRequest,
)
from misthos.services.audit import export, to_csv
from misthos.services.sso import SsoError
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


async def _sso_out(publisher_id: str) -> SsoOut:
    try:
        entitled = await run_in_threadpool(store.entitled, publisher_id, plans.Feature.SSO)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no publisher {publisher_id}") from exc
    return SsoOut(
        connection=await run_in_threadpool(store.sso, publisher_id),
        callback_url=sso_callback_url(),
        entitled=entitled,
    )


def _check_provider(publisher_id: str, terms: single_sign_on.SsoTerms) -> None:
    """Outside the simulation, a connection is kept only once its secret is in the
    secret store and its provider answers discovery: a sign-on that cannot work is
    said now, to the person setting it up, not to the next person signing in. Runs
    in the threadpool."""
    if settings.simulated:
        return
    trial = SsoConnection(
        publisher_id=publisher_id,
        issuer=terms.issuer,
        client_id=terms.client_id,
        client_secret_ref=terms.client_secret_ref,
        domains=list(terms.domains),
        configured_at=datetime.now(UTC),
    )
    try:
        oidc_client(trial).discover()
    except HTTPException as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except SsoError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/{publisher_id}/sso",
    response_model=SsoOut,
    responses={403: {"description": "Not this organisation, or not through its sign-on"}},
)
async def sso(publisher_id: str, account: Account | None = SIGNED_IN) -> SsoOut:
    """The organisation's single sign-on, the redirect URI to register with its
    provider, and whether its plan includes it."""
    require_owner_or_simulation(account, publisher_id, "see this single sign-on")
    return await _sso_out(publisher_id)


@router.put(
    "/{publisher_id}/sso",
    response_model=SsoOut,
    responses={
        402: {"description": "The plan does not include single sign-on"},
        403: {
            "description": "Not this organisation; or requiring single sign-on from a "
            "session that did not come through it"
        },
        409: {"description": "A domain already signs in to another organisation"},
        422: {
            "description": "The connection cannot work: a bad issuer or domain, the secret "
            "not in the secret store, or a provider that does not answer discovery"
        },
    },
)
async def set_sso(
    publisher_id: str,
    payload: SsoRequest,
    account: Account | None = SIGNED_IN,
    identity: Identity | None = SESSION,
) -> SsoOut:
    """Connect the organisation to its identity provider, or replace the connection.

    `required` makes single sign-on the only way to act for the organisation. It is
    turned on only from a session that came through this sign-on, so a provider that
    has never worked cannot lock the organisation out.
    """
    require_owner_or_simulation(account, publisher_id, "set up single sign-on")
    await require_plan(publisher_id, plans.Feature.SSO)
    try:
        terms = single_sign_on.terms(
            issuer=payload.issuer,
            client_id=payload.client_id,
            client_secret_ref=payload.client_secret_ref,
            domains=payload.domains,
            required=payload.required,
            local_ok=settings.simulated,
        )
    except single_sign_on.SsoRefusal as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    through_it = (
        identity is not None
        and identity.method == "sso"
        and identity.organisation == publisher_id
    )
    if terms.required and not through_it:
        raise HTTPException(
            status_code=403,
            detail="sign in through this single sign-on before requiring it, so a provider "
            "that does not work cannot lock the organisation out",
        )
    await run_in_threadpool(_check_provider, publisher_id, terms)
    try:
        await run_in_threadpool(store.set_sso, publisher_id, terms)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no publisher {publisher_id}") from exc
    except SsoDomainTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _sso_out(publisher_id)


@router.delete(
    "/{publisher_id}/sso",
    status_code=204,
    responses={204: {"description": "Removed; every session it issued has ended"}},
)
async def remove_sso(publisher_id: str, account: Account | None = SIGNED_IN) -> Response:
    """Remove the organisation's single sign-on. Every session that came through it
    stops acting for the organisation at once. Needs no plan: an organisation can
    always take a protection off from a session the protection lets in."""
    require_owner_or_simulation(account, publisher_id, "remove single sign-on")
    try:
        await run_in_threadpool(store.remove_sso, publisher_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no publisher {publisher_id}") from exc
    return Response(status_code=204)


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

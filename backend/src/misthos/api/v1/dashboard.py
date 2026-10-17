from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from misthos.api.session import (
    SIGNED_IN,
    refuse_anonymous,
    require_owner,
    signed_in_address,
)
from misthos.schemas import Account, DashboardOut
from misthos.store import store

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=DashboardOut)
async def get_dashboard(request: Request, account: Account | None = SIGNED_IN) -> DashboardOut:
    """The signed-in user's personalized dashboard view.

    Returns the publisher or contributor dashboard scoped to the authenticated party.
    Anonymous requests are refused with 401; half-onboarded sessions without role or
    party_id return 409.
    """
    address = await signed_in_address(request)
    if address is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: sign in to view your dashboard",
        )

    if account is None or not account.role or not account.party_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Role selection required: complete onboarding by choosing a role and party",
        )

    refuse_anonymous(account, "view your dashboard")
    require_owner(account, account.party_id, "view your dashboard")

    if account.role == "publisher":
        pub_dash = await run_in_threadpool(
            store.publisher_dashboard,
            account.party_id,
            account.github_login,
        )
        return DashboardOut(role="publisher", publisher=pub_dash)

    if account.role == "contributor":
        con_dash = await run_in_threadpool(
            store.contributor_dashboard,
            account.party_id,
            account.github_login,
        )
        return DashboardOut(role="contributor", contributor=con_dash)

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown account role: {account.role}",
    )

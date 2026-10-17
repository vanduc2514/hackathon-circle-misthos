"""Who is asking: the signed-in wallet, if any, and what it may do.

Writes need a signed-in account with the right role and ownership. Reads stay
public, except an organisation's or a contributor's own records.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from misthos.auth import sessions
from misthos.schemas import Account
from misthos.store import store


def token_of(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.cookies.get(sessions.COOKIE)


async def signed_in_address(request: Request) -> str | None:
    token = token_of(request)
    return sessions.address_in(token) if token else None


async def current_account(request: Request) -> Account | None:
    address = await signed_in_address(request)
    if address is None:
        return None
    return await run_in_threadpool(store.repo.get_account, address)


def refuse_anonymous(account: Account | None, what: str) -> None:
    """Nothing is written without a signed-in account."""
    if account is None:
        raise HTTPException(status_code=401, detail=f"sign in to {what}")


def require_role(account: Account | None, role: str, what: str) -> Account:
    if account is None:
        raise HTTPException(status_code=401, detail=f"sign in to {what}")
    if account.role != role:
        raise HTTPException(status_code=403, detail=f"only a {role} can {what}")
    return account


def require_github(account: Account, what: str) -> None:
    """A publisher's issues and a contributor's pull requests map to a GitHub
    identity, so neither can act before linking one (#80)."""
    if not account.github_login:
        raise HTTPException(status_code=403, detail=f"link your GitHub account to {what}")


def require_owner(account: Account | None, party_id: str, what: str) -> None:
    """The party's own records, and only to that party."""
    if account is None:
        raise HTTPException(status_code=401, detail=f"sign in to {what}")
    if account.party_id != party_id:
        raise HTTPException(status_code=403, detail=f"only {party_id} can {what}")


# The dependency, ready to use as a parameter default.
SIGNED_IN = Depends(current_account)

"""Who is asking: the signed-in identity, if any, the account it acts as, and what that
account may do.

A session proves a wallet or a GitHub account (#131), and either one finds the same
account once both are on it: a wallet by its connected address, a GitHub account by
its numeric id. Writes need a signed-in account with the right role and ownership.
The simulation also lets anonymous visitors drive the demo, because the demo has no
wallets; a deployment does not. Reads stay public, except an organisation's or a
contributor's own records.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from misthos.auth import sessions
from misthos.auth.sessions import Identity
from misthos.config import settings
from misthos.schemas import Account
from misthos.store import store


def token_of(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.cookies.get(sessions.COOKIE)


async def signed_in(request: Request) -> Identity | None:
    token = token_of(request)
    return sessions.identity_in(token) if token else None


def account_of(identity: Identity) -> Account | None:
    """The account this identity acts as, or None before it has chosen a side."""
    if identity.method == "github":
        assert identity.github_id is not None
        return store.account_by_github_id(identity.github_id)
    assert identity.address is not None
    return store.account_by_address(identity.address)


async def current_account(request: Request) -> Account | None:
    identity = await signed_in(request)
    if identity is None:
        return None
    return await run_in_threadpool(account_of, identity)


def refuse_anonymous(account: Account | None, what: str) -> None:
    """Outside the simulation, nothing is written without a signed-in account."""
    if account is None and not settings.simulated:
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


def require_owner_or_simulation(account: Account | None, party_id: str, what: str) -> None:
    """The party's own records: theirs when signed in, the demo's in the simulation."""
    if account is not None:
        if account.party_id != party_id:
            raise HTTPException(status_code=403, detail=f"only {party_id} can {what}")
        return
    if not settings.simulated:
        raise HTTPException(status_code=401, detail=f"sign in to {what}")


# The dependencies, ready to use as parameter defaults.
SIGNED_IN = Depends(current_account)
SESSION = Depends(signed_in)

"""Who is asking: the signed-in identity, if any, the account it acts as, and what that
account may do.

A session proves a wallet or a GitHub account (#131), and either one finds the same
account once both are on it: a wallet by its connected address, a GitHub account by
its numeric id. A session that came through an organisation's single sign-on (#53)
acts as that organisation's account, for as long as its connection speaks for the
person's address; an organisation that requires single sign-on is acted for in no
other way. Writes need a signed-in account with the right role and ownership.
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
    if identity.method == "sso":
        assert identity.organisation is not None and identity.email is not None
        return store.account_for_sso(identity.organisation, identity.email)
    if identity.method == "github":
        assert identity.github_id is not None
        return store.account_by_github_id(identity.github_id)
    assert identity.address is not None
    return store.account_by_address(identity.address)


def sso_unmet(identity: Identity, account: Account | None) -> bool:
    """Whether the account requires its single sign-on and this session did not come
    through it. Runs in the threadpool."""
    if account is None or identity.method == "sso":
        return False
    return store.sso_required(account.party_id)


def acting_account(identity: Identity) -> Account | None:
    """The account this identity may act as, refusing a session its organisation's
    single sign-on requirement shuts out. Runs in the threadpool."""
    account = account_of(identity)
    if sso_unmet(identity, account):
        assert account is not None
        raise HTTPException(
            status_code=403,
            detail=f"{account.party_id} acts only through its single sign-on; sign in "
            "with your work e-mail",
        )
    return account


async def current_account(request: Request) -> Account | None:
    identity = await signed_in(request)
    if identity is None:
        return None
    account = await run_in_threadpool(acting_account, identity)
    if identity.method == "sso":
        # Who acted, for the decision log: the account is the organisation's.
        request.state.sso_email = identity.email
    return account


async def viewing_account(request: Request) -> Account | None:
    """The account for a public page that shows its own records in full: a session
    its organisation's single sign-on requirement shuts out sees the page as anyone
    would, rather than being refused it."""
    identity = await signed_in(request)
    if identity is None:
        return None
    account = await run_in_threadpool(account_of, identity)
    return None if await run_in_threadpool(sso_unmet, identity, account) else account


def who(request: Request, account: Account | None, fallback: str) -> str:
    """The name the decision log records for what this request does."""
    if account is None:
        return fallback
    person = getattr(request.state, "sso_email", None)
    return person or account.github_login or account.address or account.party_id


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
VIEWER = Depends(viewing_account)
SESSION = Depends(signed_in)

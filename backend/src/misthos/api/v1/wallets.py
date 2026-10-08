"""Circle wallets for publishers and contributors (#29).

The wallets are user-controlled: the core starts a session and reads an address back,
and nothing else. The address it keeps is the one a payout goes to, so these routes are
the party's own — a signed-in account, of the kind the path names, acting on its own id.
The simulation lets a visitor try them, like every other write.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from misthos.api.guards import limit_actions
from misthos.api.session import SIGNED_IN, require_owner_or_simulation, require_role
from misthos.config import settings
from misthos.schemas import Account, Wallet, WalletSessionOut
from misthos.services.wallets import Wallets, WalletUnavailable
from misthos.store import store

router = APIRouter(prefix="/wallets", tags=["wallets"])

PartyPath = Literal["publisher", "contributor"]


async def _require_own(account: Account | None, party: PartyPath, party_id: str) -> None:
    """The party itself, and of the kind the path names.

    What this records is where the money goes, so a caller who could name any id
    could point a contributor's payout at an address of its own. Signed in, the
    account must be that party and of that role; the simulation keeps the demo
    usable by a visitor, as it does everywhere else.
    """
    if account is not None:
        require_role(account, party, f"use this {party} wallet")
    require_owner_or_simulation(account, party_id, f"use this {party} wallet")


async def _require_known(party: PartyPath, party_id: str) -> None:
    found = await run_in_threadpool(
        store.get_publisher if party == "publisher" else store.get_contributor, party_id
    )
    if found is None:
        raise HTTPException(status_code=404, detail=f"no {party} {party_id}")


def _kept(address: str) -> Wallet:
    """The wallet as it is stored: the address and the chain.

    Those are the two columns both repositories hold, and the in-memory one is the
    test double for the SQL one, so it must not keep more. The Circle user id is
    derived from the party (`services.wallets.circle_user_id`), and the session
    response carries it.
    """
    return Wallet(address=address, chain=settings.chain)


@router.post(
    "/{party}/{party_id}/session",
    response_model=WalletSessionOut,
    dependencies=[limit_actions],
)
async def start_session(
    party: PartyPath, party_id: str, account: Account | None = SIGNED_IN
) -> WalletSessionOut:
    """Start a Circle wallet session for a publisher or contributor.

    Returns the user token and, when the party has no Arc wallet yet, the PIN
    challenge their browser runs. The platform never sees the PIN or the key.
    An address that already exists is linked straight away.
    """
    await _require_own(account, party, party_id)
    await _require_known(party, party_id)
    try:
        session = await Wallets(settings).session(party, party_id)
    except WalletUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    wallet = None
    if session.address:
        wallet = store.link_wallet(party, party_id, _kept(session.address))
    return WalletSessionOut(
        party=party,
        party_id=party_id,
        circle_user_id=session.circle_user_id,
        app_id=session.app_id,
        user_token=session.user_token,
        encryption_key=session.encryption_key,
        challenge_id=session.challenge_id,
        wallet=wallet,
        simulated=session.simulated,
    )


@router.post(
    "/{party}/{party_id}/link",
    response_model=Wallet,
    dependencies=[limit_actions],
)
async def link(party: PartyPath, party_id: str, account: Account | None = SIGNED_IN) -> Wallet:
    """After the challenge completes, read the address back from Circle and keep it."""
    await _require_own(account, party, party_id)
    await _require_known(party, party_id)
    try:
        found = await Wallets(settings).wallet(party, party_id)
    except WalletUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if found.address is None:
        raise HTTPException(status_code=409, detail="the wallet has not been created yet")
    return store.link_wallet(party, party_id, _kept(found.address))

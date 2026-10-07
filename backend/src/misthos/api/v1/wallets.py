from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException

from misthos.config import settings
from misthos.schemas import Wallet, WalletSessionOut
from misthos.services.wallets import Wallets, WalletUnavailable
from misthos.store import store

router = APIRouter(prefix="/wallets", tags=["wallets"])

PartyPath = Literal["publisher", "contributor"]


def _known(party: PartyPath, party_id: str) -> None:
    known = store.publishers if party == "publisher" else store.contributors
    if party_id not in known:
        raise HTTPException(status_code=404, detail=f"no {party} {party_id}")


@router.post("/{party}/{party_id}/session", response_model=WalletSessionOut)
async def start_session(party: PartyPath, party_id: str) -> WalletSessionOut:
    """Start a Circle wallet session for a publisher or contributor.

    Returns the user token and, when the party has no Arc wallet yet, the PIN
    challenge their browser runs. The platform never sees the PIN or the key.
    An address that already exists is linked straight away.
    """
    _known(party, party_id)
    try:
        session = await Wallets(settings).session(party, party_id)
    except WalletUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    wallet = None
    if session.address:
        wallet = store.link_wallet(
            party,
            party_id,
            Wallet(
                address=session.address,
                chain=settings.chain,
                circle_user_id=session.circle_user_id,
            ),
        )
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


@router.post("/{party}/{party_id}/link", response_model=Wallet)
async def link(party: PartyPath, party_id: str) -> Wallet:
    """After the challenge completes, read the address back from Circle and keep it."""
    _known(party, party_id)
    try:
        found = await Wallets(settings).wallet(party, party_id)
    except WalletUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if found.address is None:
        raise HTTPException(status_code=409, detail="the wallet has not been created yet")
    return store.link_wallet(
        party,
        party_id,
        Wallet(address=found.address, chain=settings.chain, circle_user_id=found.circle_user_id),
    )

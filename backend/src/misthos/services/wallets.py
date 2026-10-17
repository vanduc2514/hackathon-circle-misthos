"""Circle wallets for publishers and contributors, reached through the edge.

Circle's wallet SDK is Node, so the edge service talks to Circle and the core
talks to the edge. The wallets are user-controlled: the core can start a session
and read an address, and nothing else. A payout goes to the address the user's
own wallet reports, which is the point of reading it back rather than trusting
an address typed into a form.

A Circle wallet is also a way in (#181). Signing in comes before any party exists, so
those wallets are not keyed by a party but by a random id the browser keeps, and they
are EOAs: the sign-in message is checked by recovering the address from an EIP-191
signature, which a smart-contract account's EIP-1271 signature does not allow.

The reply is checked before anything is kept. An edge configured for the other Arc
network would hand out a wallet the escrow never releases to, so a reply naming a
blockchain other than Circle's name for the API's own chain is refused (#124).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from misthos.config import Settings
from misthos.domain.network import ARC_MAINNET, ARC_TESTNET

Party = Literal["publisher", "contributor"]

# Circle's names for the two Arc networks, as the edge's CIRCLE_WALLET_BLOCKCHAIN takes
# them (ARC_TESTNET and ARC_MAINNET in edge/src/circle-wallets.ts). Circle makes these
# wallets on no other chain the API could settle on.
CIRCLE_BLOCKCHAINS: dict[int, str] = {ARC_TESTNET: "ARC-TESTNET", ARC_MAINNET: "ARC"}


@dataclass(frozen=True)
class WalletSession:
    circle_user_id: str
    app_id: str
    user_token: str
    encryption_key: str
    challenge_id: str | None
    address: str | None
    blockchain: str


@dataclass(frozen=True)
class PartyWallet:
    circle_user_id: str
    address: str | None
    blockchain: str


@dataclass(frozen=True)
class SignInSignature:
    """A sign-in message's signature, or the challenge Circle's frame runs to make it."""

    app_id: str
    user_token: str
    encryption_key: str
    challenge_id: str | None
    signature: str | None


class WalletUnavailable(RuntimeError):
    """The edge or Circle could not answer."""


class WalletRefused(WalletUnavailable):
    """The edge answered, with a wallet a live API must not keep: one it may have made
    up, or one on another chain. Nothing usable came back, as with no answer at all."""


def circle_blockchain(chain_id: int) -> str | None:
    """Circle's name for the chain, or None where Circle has no Arc wallet."""
    return CIRCLE_BLOCKCHAINS.get(chain_id)


def circle_user_id(party: Party, party_id: str) -> str:
    return f"misthos-{party}-{party_id}"


def sign_in_user_id(sign_in_id: str) -> str:
    """The Circle user behind a sign-in with a Circle wallet; no party exists yet."""
    return f"misthos-signin-{sign_in_id}"


class Wallets:
    def __init__(self, cfg: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.client = client

    @property
    def blockchain(self) -> str:
        return circle_blockchain(self.cfg.chain_id) or CIRCLE_BLOCKCHAINS[ARC_TESTNET]

    async def session(self, party: Party, party_id: str) -> WalletSession:
        body = self._live(await self._call("POST", f"/wallets/{party}/{party_id}/session"))
        return WalletSession(
            circle_user_id=body["circleUserId"],
            app_id=body["appId"],
            user_token=body["userToken"],
            encryption_key=body["encryptionKey"],
            challenge_id=body.get("challengeId"),
            address=body.get("address"),
            blockchain=body["blockchain"],
        )

    async def wallet(self, party: Party, party_id: str) -> PartyWallet:
        body = self._live(await self._call("GET", f"/wallets/{party}/{party_id}"))
        return PartyWallet(
            circle_user_id=body["circleUserId"],
            address=body.get("address"),
            blockchain=body["blockchain"],
        )

    async def sign_in_session(self, sign_in_id: str) -> WalletSession:
        """Continue a sign-in with a Circle wallet: the user token, and the PIN and wallet
        challenge while the wallet does not exist yet."""
        body = self._live(await self._call("POST", f"/wallets/sign-in/{sign_in_id}/session"))
        address = body.get("address")
        return WalletSession(
            circle_user_id=body["circleUserId"],
            app_id=body["appId"],
            user_token=body["userToken"],
            encryption_key=body["encryptionKey"],
            challenge_id=body.get("challengeId"),
            address=address.lower() if address else None,
            blockchain=body["blockchain"],
        )

    async def sign_in_wallet(self, sign_in_id: str) -> PartyWallet:
        """The sign-in wallet's address, read only: no user, PIN or wallet is created."""
        body = self._live(await self._call("GET", f"/wallets/sign-in/{sign_in_id}"))
        address = body.get("address")
        return PartyWallet(
            circle_user_id=body["circleUserId"],
            address=address.lower() if address else None,
            blockchain=body["blockchain"],
        )

    async def sign_in_signature(self, sign_in_id: str, message: str) -> SignInSignature:
        """Have the Circle wallet sign a sign-in message.

        Live, Circle answers with a challenge, and the signature is made in Circle's frame
        once the wallet's owner enters their PIN. The platform never sees it before the
        browser hands it to `POST /auth/verify`, which checks it like any other.
        """
        body = self._live(
            await self._call(
                "POST", f"/wallets/sign-in/{sign_in_id}/sign", json={"message": message}
            )
        )
        return SignInSignature(
            app_id=body["appId"],
            user_token=body["userToken"],
            encryption_key=body["encryptionKey"],
            challenge_id=body["challengeId"],
            signature=None,
        )

    def _live(self, body: dict) -> dict:
        """The edge's reply, if it is a wallet this API may keep."""
        if body.get("simulated") is True:
            raise WalletRefused("the edge answered with a wallet it did not create")
        chain_id = self.cfg.chain_id
        expected = circle_blockchain(chain_id)
        if expected is None:
            raise WalletRefused(
                f"the API settles on chain {chain_id}, where Circle has no Arc wallet to keep"
            )
        if body.get("blockchain") != expected:
            raise WalletRefused(
                f"the edge's wallet is on {body.get('blockchain')!r}, but the API settles on "
                f"{self.cfg.network.label}, chain {chain_id}, which Circle names {expected}; "
                f"set the edge's CIRCLE_WALLET_BLOCKCHAIN to {expected}"
            )
        return body

    async def _call(self, method: str, path: str, json: dict | None = None) -> dict:
        url = self.cfg.edge_url.rstrip("/") + path
        headers = {"X-Misthos-Core-Token": self.cfg.edge_core_token}
        try:
            if self.client is not None:
                response = await self.client.request(method, url, headers=headers, json=json)
            else:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.request(method, url, headers=headers, json=json)
        except httpx.HTTPError as exc:
            raise WalletUnavailable(f"edge unreachable: {exc}") from exc
        if response.status_code != 200:
            raise WalletUnavailable(f"edge answered {response.status_code}: {response.text}")
        return response.json()

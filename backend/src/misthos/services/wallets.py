"""Circle wallets for publishers and contributors, reached through the edge.

Circle's wallet SDK is Node, so the edge service talks to Circle and the core
talks to the edge. The wallets are user-controlled: the core can start a session
and read an address, and nothing else. A payout goes to the address the user's
own wallet reports, which is the point of reading it back rather than trusting
an address typed into a form.

Simulated, no request leaves the process: each party gets a stable address
derived from its id, the same derivation the edge uses, so the two agree.

Live, the reply is checked before anything is kept. A simulated edge hands out those
same derived addresses, which nobody holds a key for, and an edge configured for the
other Arc network hands out a wallet the escrow never releases to. Both happened when
the edge read `MISTHOS_SIMULATED=0` as the simulation while the API read it as live
(#124), so a live API refuses a reply that does not say it is live, or that names a
blockchain other than Circle's name for the API's own chain.
"""

from __future__ import annotations

import hashlib
import json
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
    simulated: bool


@dataclass(frozen=True)
class PartyWallet:
    circle_user_id: str
    address: str | None
    blockchain: str
    simulated: bool


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


def simulated_address(party: Party, party_id: str) -> str:
    digest = hashlib.sha256(circle_user_id(party, party_id).encode()).hexdigest()
    return f"0x{digest[:40]}"


class Wallets:
    def __init__(self, cfg: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.client = client

    @property
    def blockchain(self) -> str:
        """What the simulation labels its made-up wallets with."""
        return circle_blockchain(self.cfg.chain_id) or CIRCLE_BLOCKCHAINS[ARC_TESTNET]

    async def session(self, party: Party, party_id: str) -> WalletSession:
        if self.cfg.simulated:
            return WalletSession(
                circle_user_id=circle_user_id(party, party_id),
                app_id="simulated",
                user_token="simulated",
                encryption_key="simulated",
                challenge_id=None,
                address=simulated_address(party, party_id),
                blockchain=self.blockchain,
                simulated=True,
            )
        body = self._live(await self._call("POST", f"/wallets/{party}/{party_id}/session"))
        return WalletSession(
            circle_user_id=body["circleUserId"],
            app_id=body["appId"],
            user_token=body["userToken"],
            encryption_key=body["encryptionKey"],
            challenge_id=body.get("challengeId"),
            address=body.get("address"),
            blockchain=body["blockchain"],
            simulated=False,
        )

    async def wallet(self, party: Party, party_id: str) -> PartyWallet:
        if self.cfg.simulated:
            return PartyWallet(
                circle_user_id=circle_user_id(party, party_id),
                address=simulated_address(party, party_id),
                blockchain=self.blockchain,
                simulated=True,
            )
        body = self._live(await self._call("GET", f"/wallets/{party}/{party_id}"))
        return PartyWallet(
            circle_user_id=body["circleUserId"],
            address=body.get("address"),
            blockchain=body["blockchain"],
            simulated=False,
        )

    def _live(self, body: dict) -> dict:
        """The edge's reply, if it is a wallet this live API may keep."""
        simulated = body.get("simulated")
        if simulated is not False:
            said = "a simulated wallet" if simulated is True else "no live wallet"
            raise WalletRefused(
                f"the edge answered with {said} (simulated: {json.dumps(simulated)}) while "
                "the API is live; run the edge with the API's MISTHOS_SIMULATED"
            )
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

    async def _call(self, method: str, path: str) -> dict:
        url = self.cfg.edge_url.rstrip("/") + path
        headers = {"X-Misthos-Core-Token": self.cfg.edge_core_token}
        try:
            if self.client is not None:
                response = await self.client.request(method, url, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.request(method, url, headers=headers)
        except httpx.HTTPError as exc:
            raise WalletUnavailable(f"edge unreachable: {exc}") from exc
        if response.status_code != 200:
            raise WalletUnavailable(f"edge answered {response.status_code}: {response.text}")
        return response.json()

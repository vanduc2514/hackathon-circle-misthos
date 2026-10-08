"""Circle wallets for publishers and contributors, reached through the edge.

Circle's wallet SDK is Node, so the edge service talks to Circle and the core
talks to the edge. The wallets are user-controlled: the core can start a session
and read an address, and nothing else. A payout goes to the address the user's
own wallet reports, which is the point of reading it back rather than trusting
an address typed into a form.

Simulated, no request leaves the process: each party gets a stable address
derived from its id, the same derivation the edge uses, so the two agree.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

import httpx

from misthos.config import Settings

Party = Literal["publisher", "contributor"]


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
        return "ARC-TESTNET" if self.cfg.chain_id != 5042 else "ARC"

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
        body = await self._call("POST", f"/wallets/{party}/{party_id}/session")
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
        body = await self._call("GET", f"/wallets/{party}/{party_id}")
        return PartyWallet(
            circle_user_id=body["circleUserId"],
            address=body.get("address"),
            blockchain=body["blockchain"],
            simulated=False,
        )

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

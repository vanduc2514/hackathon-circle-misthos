"""Test helpers for wallet address derivations."""

from __future__ import annotations

import hashlib
from typing import Literal

from misthos.config import Settings
from misthos.services.wallets import PartyWallet, WalletSession, circle_blockchain

Party = Literal["publisher", "contributor"]


def circle_user_id(party: Party, party_id: str) -> str:
    return f"misthos-{party}-{party_id}"


def simulated_address(party: Party, party_id: str) -> str:
    digest = hashlib.sha256(circle_user_id(party, party_id).encode()).hexdigest()
    return f"0x{digest[:40]}"


class SimulatedWallets:
    def __init__(self, cfg: Settings, client: object = None) -> None:
        self.cfg = cfg

    async def session(self, party: Party, party_id: str) -> WalletSession:
        blockchain = circle_blockchain(self.cfg.chain_id) or "ARC-TESTNET"
        return WalletSession(
            circle_user_id=circle_user_id(party, party_id),
            app_id="simulated",
            user_token="simulated",
            encryption_key="simulated",
            challenge_id=None,
            address=simulated_address(party, party_id),
            blockchain=blockchain,
        )

    async def wallet(self, party: Party, party_id: str) -> PartyWallet:
        blockchain = circle_blockchain(self.cfg.chain_id) or "ARC-TESTNET"
        return PartyWallet(
            circle_user_id=circle_user_id(party, party_id),
            address=simulated_address(party, party_id),
            blockchain=blockchain,
        )

"""The edge wallet boundary, faked for tests."""

from __future__ import annotations

import hashlib

from misthos.services.wallets import PartyWallet, WalletSession


def simulated_address(party: str, party_id: str) -> str:
    digest = hashlib.sha256(f"misthos-{party}-{party_id}".encode()).hexdigest()
    return f"0x{digest[:40]}"


class FakeWallets:
    blockchain = "ARC-TESTNET"

    async def session(self, party: str, party_id: str) -> WalletSession:
        return WalletSession(
            circle_user_id=f"misthos-{party}-{party_id}",
            app_id="app-test",
            user_token="token-test",
            encryption_key="key-test",
            challenge_id=None,
            address=simulated_address(party, party_id),
            blockchain=self.blockchain,
        )

    async def wallet(self, party: str, party_id: str) -> PartyWallet:
        return PartyWallet(
            circle_user_id=f"misthos-{party}-{party_id}",
            address=simulated_address(party, party_id),
            blockchain=self.blockchain,
        )

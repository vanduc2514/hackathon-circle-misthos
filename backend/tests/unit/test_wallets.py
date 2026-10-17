"""Circle wallets through the edge: what the core sends and what it keeps."""

from __future__ import annotations

import json

import httpx
import pytest

from misthos.config import Settings
from misthos.domain.network import ARC_MAINNET, ARC_TESTNET
from misthos.services.wallets import (
    PartyWallet,
    WalletRefused,
    Wallets,
    WalletSession,
    WalletUnavailable,
    circle_blockchain,
)

LIVE = Settings(edge_url="http://edge", edge_core_token="secret")


def _edge(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _reply(**changes: object) -> dict:
    """What a live edge answers, as edge/src/circle-wallets.ts builds it, on Arc testnet."""
    body = {
        "circleUserId": "misthos-contributor-CON-1",
        "appId": "app-1",
        "userToken": "ut",
        "encryptionKey": "ek",
        "challengeId": None,
        "address": "0x00000000000000000000000000000000000000b0",
        "blockchain": "ARC-TESTNET",
    }
    return body | changes


async def _ask(cfg: Settings, op: str, body: dict) -> WalletSession | PartyWallet:
    async with _edge(lambda _: httpx.Response(200, json=body)) as client:
        wallets = Wallets(cfg, client)
        if op == "session":
            return await wallets.session("contributor", "CON-1")
        return await wallets.wallet("contributor", "CON-1")


class TestLive:
    async def test_a_session_carries_the_core_token_and_returns_the_challenge(self) -> None:
        seen: dict = {}

        def answer(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["token"] = request.headers.get("X-Misthos-Core-Token")
            return httpx.Response(
                200,
                json={
                    "circleUserId": "misthos-contributor-CON-1",
                    "appId": "app-1",
                    "userToken": "ut",
                    "encryptionKey": "ek",
                    "challengeId": "ch-1",
                    "address": None,
                    "blockchain": "ARC-TESTNET",
                            },
            )

        async with _edge(answer) as client:
            session = await Wallets(LIVE, client).session("contributor", "CON-1")

        assert seen == {"url": "http://edge/wallets/contributor/CON-1/session", "token": "secret"}
        assert session.challenge_id == "ch-1" and session.address is None

    async def test_an_edge_refusal_is_raised_not_treated_as_no_wallet(self) -> None:
        async with _edge(lambda _: httpx.Response(401, json={"error": "core token required"})) as c:
            with pytest.raises(WalletUnavailable):
                await Wallets(LIVE, c).wallet("publisher", "PUB-1")

    async def test_an_unreachable_edge_is_raised(self) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        async with _edge(down) as client:
            with pytest.raises(WalletUnavailable):
                await Wallets(LIVE, client).session("publisher", "PUB-1")

    async def test_reads_the_wallet_address_back(self) -> None:
        body = {
            "circleUserId": "misthos-publisher-PUB-1",
            "address": "0xB0B",
            "blockchain": "ARC-TESTNET",
            }
        async with _edge(lambda _: httpx.Response(200, content=json.dumps(body))) as client:
            wallet = await Wallets(LIVE, client).wallet("publisher", "PUB-1")
        assert wallet.address == "0xB0B"


class TestWhatALiveApiKeeps:
    """A live API keeps only a live wallet on its own chain (#124).

    The API refuses a reply that says it is not a real wallet, and compares the reply's
    `blockchain` with the chain it settles on. A payout to an address nobody holds a key
    for would be lost.
    """

    def test_circle_names_the_two_arc_networks_and_nothing_else(self) -> None:
        # The names edge/src/circle-wallets.ts passes to Circle (CIRCLE_WALLET_BLOCKCHAIN).
        assert circle_blockchain(ARC_TESTNET) == "ARC-TESTNET"
        assert circle_blockchain(ARC_MAINNET) == "ARC"
        assert circle_blockchain(1) is None

    @pytest.mark.parametrize("op", ["session", "wallet"])
    async def test_a_wallet_the_edge_made_up_is_refused(self, op: str) -> None:
        with pytest.raises(WalletRefused, match="did not create"):
            await _ask(LIVE, op, _reply(simulated=True))

    @pytest.mark.parametrize("op", ["session", "wallet"])
    @pytest.mark.parametrize(
        ("chain_id", "blockchain"),
        [(ARC_TESTNET, "ARC"), (ARC_MAINNET, "ARC-TESTNET"), (ARC_TESTNET, None)],
    )
    async def test_a_wallet_on_another_chain_is_refused(
        self, op: str, chain_id: int, blockchain: str | None
    ) -> None:
        cfg = LIVE.model_copy(update={"chain_id": chain_id})
        with pytest.raises(WalletRefused, match=f"chain {chain_id}"):
            await _ask(cfg, op, _reply(blockchain=blockchain))

    @pytest.mark.parametrize("op", ["session", "wallet"])
    async def test_no_wallet_is_kept_on_a_chain_circle_has_no_arc_wallet_for(self, op: str) -> None:
        cfg = LIVE.model_copy(update={"chain_id": 1})
        with pytest.raises(WalletRefused, match="chain 1"):
            await _ask(cfg, op, _reply())

    @pytest.mark.parametrize("op", ["session", "wallet"])
    @pytest.mark.parametrize(
        ("chain_id", "blockchain"), [(ARC_TESTNET, "ARC-TESTNET"), (ARC_MAINNET, "ARC")]
    )
    async def test_a_live_wallet_on_the_api_chain_is_kept(
        self, op: str, chain_id: int, blockchain: str
    ) -> None:
        cfg = LIVE.model_copy(update={"chain_id": chain_id})
        found = await _ask(cfg, op, _reply(blockchain=blockchain))
        assert found.address == "0x00000000000000000000000000000000000000b0"
        assert found.blockchain == blockchain



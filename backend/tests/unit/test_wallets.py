"""Circle wallets through the edge: what the core sends and what it keeps."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from misthos.config import Settings
from misthos.services.wallets import Wallets, WalletUnavailable, simulated_address

LIVE = Settings(simulated=False, edge_url="http://edge", edge_core_token="secret")


def _edge(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestSimulated:
    async def test_no_request_leaves_the_process(self) -> None:
        def refuse(_: httpx.Request) -> httpx.Response:
            raise AssertionError("simulated wallets must not call the edge")

        async with _edge(refuse) as client:
            wallets = Wallets(Settings(simulated=True), client)
            session = await wallets.session("contributor", "CON-1")
        assert session.simulated and session.challenge_id is None
        assert session.address == simulated_address("contributor", "CON-1")

    def test_the_simulated_address_matches_the_edge_derivation(self) -> None:
        # sha256("misthos-contributor-CON-1")[:40], as edge/src/circle-wallets.ts derives it.
        digest = hashlib.sha256(b"misthos-contributor-CON-1").hexdigest()
        assert simulated_address("contributor", "CON-1") == "0x" + digest[:40]


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

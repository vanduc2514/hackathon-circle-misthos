"""A live API keeps a party's payout wallet only from a live edge on its own chain.

With `MISTHOS_SIMULATED=0` the API ran live while the edge's wallet routes stayed
simulated, and the API kept the made-up address they handed out as where the party's
payouts go (#124). These drive the real routes against an edge answered by
httpx.MockTransport, signed in as the party, as a live deployment requires.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from siwe_wallet import Wallet

from misthos.api.v1 import wallets as wallets_api
from misthos.config import Settings, settings
from misthos.domain.network import ARC_MAINNET, ARC_TESTNET
from misthos.main import app
from misthos.services.wallets import Wallets
from misthos.store import store

API = "/api/v1"
ADDRESS = "0x00000000000000000000000000000000000000c1"


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def sign_in_contributor(client: TestClient, seed: int) -> str:
    wallet = Wallet(seed)
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    verified = client.post(
        f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)}
    )
    assert verified.status_code == 200, verified.text
    account = client.post(f"{API}/auth/role", json={"role": "contributor", "name": "erin"})
    assert account.status_code == 201, account.text
    return account.json()["party_id"]


def live_edge(monkeypatch: pytest.MonkeyPatch, reply: dict, chain_id: int = ARC_TESTNET) -> None:
    """Run the API live on `chain_id`, with an edge that answers `reply` to the core."""
    monkeypatch.setattr(settings, "simulated", False)
    monkeypatch.setattr(settings, "chain_id", chain_id)
    monkeypatch.setattr(settings, "edge_core_token", "secret")

    def edge(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Misthos-Core-Token"] == "secret"
        return httpx.Response(200, json=reply)

    transport = httpx.MockTransport(edge)

    def wallets(cfg: Settings) -> Wallets:
        return Wallets(cfg, httpx.AsyncClient(transport=transport))

    monkeypatch.setattr(wallets_api, "Wallets", wallets)


def reply(party_id: str, **changes: object) -> dict:
    """A live edge's answer, as edge/src/circle-wallets.ts builds it, for an Arc wallet."""
    body = {
        "circleUserId": f"misthos-contributor-{party_id}",
        "appId": "app-1",
        "userToken": "ut",
        "encryptionKey": "ek",
        "challengeId": None,
        "address": ADDRESS,
        "blockchain": "ARC-TESTNET",
        "simulated": False,
    }
    return body | changes


def kept(party_id: str) -> object:
    contributor = store.get_contributor(party_id)
    assert contributor is not None
    return contributor.wallet


class TestALiveApi:
    def test_refuses_a_wallet_the_edge_made_up_and_keeps_nothing(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        party_id = sign_in_contributor(client, 81)
        before = kept(party_id)
        # What a simulated edge answers: a stable address derived from the id, no PIN.
        live_edge(monkeypatch, reply(party_id, appId="simulated", simulated=True))

        session = client.post(f"{API}/wallets/contributor/{party_id}/session")
        link = client.post(f"{API}/wallets/contributor/{party_id}/link")

        for answer in (session, link):
            assert answer.status_code == 502, answer.text
            assert "simulated" in answer.json()["detail"]
        assert kept(party_id) == before

    @pytest.mark.parametrize(
        ("chain_id", "blockchain"), [(ARC_TESTNET, "ARC"), (ARC_MAINNET, "ARC-TESTNET"), (1, "ARC")]
    )
    def test_refuses_a_wallet_on_another_chain_and_keeps_nothing(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        chain_id: int,
        blockchain: str,
    ) -> None:
        party_id = sign_in_contributor(client, 82)
        before = kept(party_id)
        live_edge(monkeypatch, reply(party_id, blockchain=blockchain), chain_id=chain_id)

        session = client.post(f"{API}/wallets/contributor/{party_id}/session")
        link = client.post(f"{API}/wallets/contributor/{party_id}/link")

        for answer in (session, link):
            assert answer.status_code == 502, answer.text
            assert f"chain {chain_id}" in answer.json()["detail"]
        assert kept(party_id) == before

    @pytest.mark.parametrize(
        ("chain_id", "blockchain", "chain"),
        [(ARC_TESTNET, "ARC-TESTNET", "arc-testnet"), (ARC_MAINNET, "ARC", "arc-mainnet")],
    )
    def test_keeps_a_live_wallet_on_its_own_chain(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        chain_id: int,
        blockchain: str,
        chain: str,
    ) -> None:
        party_id = sign_in_contributor(client, 83)
        live_edge(monkeypatch, reply(party_id, blockchain=blockchain), chain_id=chain_id)

        session = client.post(f"{API}/wallets/contributor/{party_id}/session")

        assert session.status_code == 200, session.text
        body = session.json()
        assert body["simulated"] is False
        assert body["wallet"] == {"address": ADDRESS, "chain": chain, "circle_user_id": None}
        assert kept(party_id).address == ADDRESS  # type: ignore[attr-defined]
        link = client.post(f"{API}/wallets/contributor/{party_id}/link")
        assert link.status_code == 200, link.text
        assert link.json()["address"] == ADDRESS

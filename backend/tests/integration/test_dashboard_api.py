from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from siwe_wallet import Wallet

from misthos.config import settings
from misthos.main import app
from misthos.store import store

API = "/api/v1"


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def sign_in(client: TestClient, seed: int, role: str, name: str, login: str) -> dict:
    wallet = Wallet(seed)
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    verified = client.post(
        f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)}
    )
    assert verified.status_code == 200, verified.text
    account = client.post(f"{API}/auth/role", json={"role": role, "name": name})
    assert account.status_code == 201, account.text
    linked = client.post(f"{API}/auth/github/simulate", json={"login": login})
    assert linked.status_code == 200, linked.text
    return linked.json()


def test_anonymous_request_is_refused_with_401(client: TestClient):
    # Simulated mode is True by default in dev/test
    assert settings.simulated is True
    res = client.get(f"{API}/dashboard")
    assert res.status_code == 401
    assert "Authentication required" in res.json()["detail"]


def test_anonymous_request_is_refused_with_401_when_not_simulated(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "simulated", False)
    res = client.get(f"{API}/dashboard")
    assert res.status_code == 401


def test_half_onboarded_request_returns_409(client: TestClient):
    # Wallet verified, but no role selected
    wallet = Wallet(12345)
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    verified = client.post(
        f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)}
    )
    assert verified.status_code == 200

    # Request dashboard before picking role
    res = client.get(f"{API}/dashboard")
    assert res.status_code == 409
    assert "Role selection required" in res.json()["detail"]


def test_publisher_dashboard_flow(client: TestClient):
    pub_account = sign_in(client, seed=1001, role="publisher", name="Acme Corp", login="acme-bot")
    assert pub_account["role"] == "publisher"
    party_id = pub_account["party_id"]

    res = client.get(f"{API}/dashboard")
    assert res.status_code == 200
    data = res.json()
    assert data["role"] == "publisher"
    assert data["contributor"] is None
    assert data["publisher"] is not None

    pub = data["publisher"]
    assert pub["party_id"] == party_id
    assert pub["name"] == "Acme Corp"
    assert pub["github_login"] == "acme-bot"
    assert "budget_remaining_usdc" in pub
    assert "committed_escrow_usdc" in pub
    assert "released_usdc" in pub
    assert isinstance(pub["action_needed"], list)
    assert isinstance(pub["all_issues"], list)


def test_contributor_dashboard_flow(client: TestClient):
    con_account = sign_in(client, seed=2002, role="contributor", name="DevDan", login="dev-dan")
    assert con_account["role"] == "contributor"
    party_id = con_account["party_id"]

    res = client.get(f"{API}/dashboard")
    assert res.status_code == 200
    data = res.json()
    assert data["role"] == "contributor"
    assert data["publisher"] is None
    assert data["contributor"] is not None

    con = data["contributor"]
    assert con["party_id"] == party_id
    assert con["handle"] == "dev-dan"
    assert con["github_login"] == "dev-dan"
    assert "owed_in_flight_usdc" in con
    assert "about_to_release_usdc" in con
    assert "paid_this_year_usdc" in con
    assert "total_earned_usdc" in con
    assert "payout_wallet_set" in con
    assert isinstance(con["action_needed"], list)
    assert isinstance(con["all_work"], list)

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from siwe_wallet import Wallet

from misthos.main import app
from tests.support.accounts import sign_in_party

pytestmark = pytest.mark.real_auth

API = "/api/v1"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_anonymous_request_is_refused_with_401(client: TestClient):
    res = client.get(f"{API}/dashboard")
    assert res.status_code == 401
    assert "Authentication required" in res.json()["detail"]


def test_half_onboarded_request_returns_409(client: TestClient):
    # Wallet verified, but no role selected (unseeded seed)
    wallet = Wallet(99999)
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
    sign_in_party(client, "PUB-1")

    res = client.get(f"{API}/dashboard")
    assert res.status_code == 200
    data = res.json()
    assert data["role"] == "publisher"
    assert data["publisher"] is not None
    assert data["publisher"]["party_id"] == "PUB-1"
    assert data["publisher"]["budget_remaining_usdc"] >= 0
    assert isinstance(data["publisher"]["action_needed"], list)
    assert isinstance(data["publisher"]["all_issues"], list)


def test_contributor_dashboard_flow(client: TestClient):
    sign_in_party(client, "CON-1")

    res = client.get(f"{API}/dashboard")
    assert res.status_code == 200
    data = res.json()
    assert data["role"] == "contributor"
    assert data["contributor"] is not None
    assert data["contributor"]["party_id"] == "CON-1"
    assert isinstance(data["contributor"]["action_needed"], list)
    assert isinstance(data["contributor"]["all_work"], list)

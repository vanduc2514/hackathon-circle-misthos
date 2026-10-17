"""The sign-in gate (#181): a wallet, a GitHub account, then a side.

The gate links GitHub before the side is chosen, and choosing the side is what creates
the account. So a login is held against the wallet in between, takes the
one-login-one-wallet slot while it is held, and is adopted onto the account when it is
created. The first step has two doors: a wallet extension such as MetaMask, and a Circle
wallet, which signs the same message and is verified the same way.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from siwe_wallet import Wallet

from misthos.api.v1 import auth as auth_routes
from misthos.auth.siwe import checksum
from misthos.config import Settings, settings
from misthos.main import app
from misthos.services.github.oauth import GitHubOAuth
from misthos.services.wallets import Wallets

API = "/api/v1"
SIGN_IN_ID = "browser-7f3a9c2e5b1d4e8f"


def sign_in(client: TestClient, wallet: Wallet) -> dict:
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    r = client.post(
        f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)}
    )
    assert r.status_code == 200, r.text
    return r.json()


def me(client: TestClient) -> dict:
    r = client.get(f"{API}/auth/me")
    assert r.status_code == 200, r.text
    return r.json()


def link(client: TestClient, login: str) -> httpx.Response:
    return client.post(f"{API}/auth/github/simulate", json={"login": login})


def choose(client: TestClient, role: str, name: str, **extra: str) -> httpx.Response:
    return client.post(f"{API}/auth/role", json={"role": role, "name": name, **extra})


def oauth_answering(monkeypatch: pytest.MonkeyPatch, login: str) -> None:
    """A configured OAuth App, with GitHub answering that the approver is `login`."""

    def github(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "gho_once"})
        return httpx.Response(200, json={"login": login})

    client = GitHubOAuth("c", "s", transport=httpx.MockTransport(github))
    monkeypatch.setattr(auth_routes, "oauth", lambda: client)


def through_github(client: TestClient) -> httpx.Response:
    """Start a link, and come back from GitHub with the state it was given."""
    start = client.post(f"{API}/auth/github/start")
    assert start.status_code == 200, start.text
    state = parse_qs(urlsplit(start.json()["authorize_url"]).query)["state"][0]
    return client.get(
        f"{API}/auth/github/callback",
        params={"code": "c0de", "state": state},
        follow_redirects=False,
    )


class TestComingBackFromGitHub:
    def test_saying_no_on_github_comes_back_to_the_gate_with_nothing_linked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        oauth_answering(monkeypatch, "never-asked")
        client = TestClient(app)
        sign_in(client, Wallet(105))
        start = client.post(f"{API}/auth/github/start").json()["authorize_url"]
        state = parse_qs(urlsplit(start).query)["state"][0]

        # What GitHub sends back when the account holder cancels: an error, no code.
        back = client.get(
            f"{API}/auth/github/callback",
            params={"error": "access_denied", "state": state},
            follow_redirects=False,
        )

        assert back.status_code == 303
        assert back.headers["location"] == "http://localhost:5173/?signin=continue&github=cancelled"
        assert me(client)["pending_github_login"] is None


class TestTheCircleDoorLive:
    """Live, Circle's frame signs: the API hands it the challenge and never the key."""

    CIRCLE_WALLET = Wallet(151)
    """Stands in for the EOA Circle keeps half of; the test signs as the frame would."""

    @pytest.fixture
    def edge(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict | None]]:
        monkeypatch.setattr(settings, "edge_core_token", "secret")
        calls: list[tuple[str, dict | None]] = []

        def answer(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-Misthos-Core-Token"] == "secret"
            body = json.loads(request.content) if request.content else None
            calls.append((request.url.path, body))
            common = {
                "circleUserId": f"misthos-signin-{SIGN_IN_ID}",
                "appId": "app-1",
                "userToken": "ut",
                "encryptionKey": "ek",
                "blockchain": "ARC-TESTNET",
            }
            calls[-1] = (f"{request.method} {request.url.path}", body)
            address = self.CIRCLE_WALLET.address.lower()
            if request.url.path.endswith("/session"):
                return httpx.Response(200, json=common | {"challengeId": None, "address": address})
            if request.method == "GET":
                return httpx.Response(200, json=common | {"address": address})
            return httpx.Response(200, json=common | {"challengeId": "sign-challenge-1"})

        transport = httpx.MockTransport(answer)

        def wallets(cfg: Settings) -> Wallets:
            return Wallets(cfg, httpx.AsyncClient(transport=transport))

        monkeypatch.setattr(auth_routes, "Wallets", wallets)
        return calls

    def test_the_frame_signs_and_verify_recovers_the_circle_wallet(
        self, edge: list[tuple[str, dict | None]]
    ) -> None:
        client = TestClient(app)
        started = client.post(f"{API}/auth/circle/session", json={"sign_in_id": SIGN_IN_ID})
        address = started.json()["address"]
        nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
        message = self.CIRCLE_WALLET.message(nonce, address=checksum(address))

        signed = client.post(
            f"{API}/auth/circle/sign", json={"sign_in_id": SIGN_IN_ID, "message": message}
        )

        assert signed.status_code == 200, signed.text
        assert signed.json() | {"user_token": None} == {
            "app_id": "app-1",
            "user_token": None,
            "encryption_key": "ek",
            "challenge_id": "sign-challenge-1",
            "signature": None,
        }
        # The address was read back, not a second session started, then the message sent.
        assert edge[-2:] == [
            (f"GET /wallets/sign-in/{SIGN_IN_ID}", None),
            (f"POST /wallets/sign-in/{SIGN_IN_ID}/sign", {"message": message}),
        ]
        # What Circle's frame hands back once the PIN is entered.
        signature = self.CIRCLE_WALLET.sign(message)
        session = client.post(
            f"{API}/auth/verify", json={"message": message, "signature": signature}
        ).json()
        assert session["address"] == self.CIRCLE_WALLET.address.lower()

    def test_reading_the_wallet_back_starts_nothing_at_circle(
        self, edge: list[tuple[str, dict | None]]
    ) -> None:
        r = TestClient(app).post(f"{API}/auth/circle/wallet", json={"sign_in_id": SIGN_IN_ID})

        assert r.status_code == 200, r.text
        assert r.json() == {"address": self.CIRCLE_WALLET.address.lower()}
        assert edge == [(f"GET /wallets/sign-in/{SIGN_IN_ID}", None)]

    def test_without_an_edge_the_circle_door_says_what_to_set_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "edge_core_token", "")
        client = TestClient(app)
        assert client.get(f"{API}/health").json()["circle_sign_in"] is False

        r = client.post(f"{API}/auth/circle/session", json={"sign_in_id": SIGN_IN_ID})

        assert r.status_code == 503
        for needed in ("MISTHOS_EDGE_CORE_TOKEN", "CIRCLE_API_KEY"):
            assert needed in r.json()["detail"]

    def test_without_an_edge_the_circle_door_is_closed(self) -> None:
        assert TestClient(app).get(f"{API}/health").json()["circle_sign_in"] is False

    def test_a_login_is_linked_only_through_github(self) -> None:
        client = TestClient(app)
        sign_in(client, Wallet(152))
        assert link(client, "typed-login").status_code == 404
        assert me(client)["pending_github_login"] is None

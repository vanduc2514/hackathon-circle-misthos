"""Sign in with GitHub, and connect a wallet later (#131).

GitHub sign-in is the main way in, a wallet the second. An account is no longer a
wallet: it has a GitHub identity, a wallet, or both, and it needs a wallet only when
money is about to move. These tests follow the issue's acceptance criteria, against a
fake GitHub on `httpx.MockTransport` that answers as the real one does.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from siwe_wallet import Wallet

from misthos.api.v1 import auth as auth_routes
from misthos.api.v1.webhooks import signature_for
from misthos.auth import sessions
from misthos.config import settings
from misthos.domain.issue import IssueState
from misthos.main import app
from misthos.observability import logs
from misthos.services.github import SimulatedGitHub
from misthos.services.github.oauth import GitHubOAuth, simulated_user
from misthos.store import Store, store

API = "/api/v1"
PUBLIC = "http://localhost:5173"
CALLBACK = f"{PUBLIC}/api/v1/auth/github/callback"


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


class FakeGitHub:
    """GitHub's OAuth endpoints for one user at a time, refusing what the real one
    refuses: another app's credentials, another callback, another token."""

    def __init__(self) -> None:
        self.user = {"id": 583231, "login": "octo-dev"}
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.path == "/login/oauth/access_token":
            form = parse_qs(request.content.decode())
            expected = {
                "client_id": ["Iv1.local"], "client_secret": ["local-secret"],
                "code": ["c0de"], "redirect_uri": [CALLBACK],
            }  # fmt: skip
            if form != expected:
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": "gho_once"})
        if request.headers.get("Authorization") != "Bearer gho_once":
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(200, json=self.user)


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    """An OAuth App configured as the README says."""
    monkeypatch.setattr(settings, "github_oauth_client_id", "Iv1.local")
    monkeypatch.setattr(settings, "github_oauth_client_secret", "local-secret")
    fake = FakeGitHub()
    monkeypatch.setattr(
        auth_routes, "GitHubOAuth", partial(GitHubOAuth, transport=httpx.MockTransport(fake))
    )
    return fake


def start_signin(client: TestClient) -> str:
    """Press "Sign in with GitHub" from the web app; the state GitHub is sent with."""
    r = client.post(f"{API}/auth/github/signin", headers={"Origin": PUBLIC})
    assert r.status_code == 200, r.text
    authorize = urlsplit(r.json()["authorize_url"])
    query = parse_qs(authorize.query)
    assert (authorize.netloc, authorize.path) == ("github.com", "/login/oauth/authorize")
    assert query["client_id"] == ["Iv1.local"] and query["redirect_uri"] == [CALLBACK]
    return query["state"][0]


def come_back(client: TestClient, state: str) -> httpx.Response:
    """GitHub sends the browser back with a code and the state it was given."""
    return client.get(
        f"{API}/auth/github/callback",
        params={"code": "c0de", "state": state},
        follow_redirects=False,
    )


def github_sign_in(client: TestClient) -> None:
    back = come_back(client, start_signin(client))
    assert back.status_code == 303, back.text


def wallet_sign_in(client: TestClient, wallet: Wallet) -> dict:
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    r = client.post(
        f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)}
    )
    assert r.status_code == 200, r.text
    return r.json()


def signed(client: TestClient, wallet: Wallet) -> dict:
    """A message over a fresh nonce, signed by the wallet: the body of a connect."""
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    return {"message": message, "signature": wallet.sign(message)}


def connect(client: TestClient, wallet: Wallet) -> httpx.Response:
    return client.post(f"{API}/auth/wallet/connect", json=signed(client, wallet))


def demo_sign_in(client: TestClient, login: str) -> dict:
    r = client.post(f"{API}/auth/github/simulate-signin", json={"login": login})
    assert r.status_code == 200, r.text
    return r.json()


def choose(client: TestClient, role: str, name: str = "Acme") -> dict:
    r = client.post(f"{API}/auth/role", json={"role": role, "name": name})
    assert r.status_code == 201, r.text
    return r.json()


def session_cookie_set(response: httpx.Response) -> bool:
    return any(
        h.lower().startswith(f"{sessions.COOKIE}=") for h in response.headers.get_list("set-cookie")
    )


class TestSigningInWithGitHub:
    """Criterion 1: with an OAuth App configured, Sign in with GitHub signs a new user
    in, and they choose a side and land on their account without a wallet."""

    def test_a_new_user_signs_in_with_github_and_chooses_a_side_without_a_wallet(
        self, github: FakeGitHub
    ) -> None:
        client = TestClient(app)
        assert client.get(f"{API}/health").json()["github_oauth"] is True

        r = client.post(f"{API}/auth/github/signin", headers={"Origin": PUBLIC})
        assert r.status_code == 200, r.text
        # The same state, kept in this browser, where the callback looks for it.
        cookie = next(
            h for h in r.headers.get_list("set-cookie") if h.startswith("misthos_github_state=")
        ).lower()
        assert "httponly" in cookie and "samesite=lax" in cookie
        assert "path=/api/v1/auth/github" in cookie and "max-age=600" in cookie
        assert "secure" not in cookie  # http://localhost, where a Secure cookie is dropped
        state = parse_qs(urlsplit(r.json()["authorize_url"]).query)["state"][0]
        assert client.cookies.get("misthos_github_state") == state

        back = come_back(client, state)

        assert back.status_code == 303, back.text
        assert back.headers["location"] == f"{PUBLIC}/account?signed_in=github"
        assert session_cookie_set(back)
        assert client.cookies.get("misthos_github_state") is None  # cleared
        assert [q.url.path for q in github.seen] == ["/login/oauth/access_token", "/user"]

        me = client.get(f"{API}/auth/me").json()
        assert me["method"] == "github"
        assert (me["github_id"], me["github_login"]) == (583231, "octo-dev")
        assert me["address"] is None and me["wallet"] is None and me["account"] is None

        account = choose(client, "contributor", "Octo")
        assert account["github_id"] == 583231 and account["github_login"] == "octo-dev"
        assert account["address"] is None
        # A contributor signed in with GitHub is known by their login.
        contributor = store.get_contributor(account["party_id"])
        assert contributor is not None and contributor.handle == "octo-dev"
        assert contributor.wallet is None
        me = client.get(f"{API}/auth/me").json()
        assert me["account"]["party_id"] == account["party_id"]
        twice = client.post(f"{API}/auth/role", json={"role": "publisher", "name": "x"})
        assert twice.status_code == 409

    def test_the_same_github_user_signs_in_to_the_same_account_after_a_rename(
        self, github: FakeGitHub
    ) -> None:
        first = TestClient(app)
        github_sign_in(first)
        account = choose(first, "contributor", "Octo")

        # The login is renamed on GitHub; the numeric id is the same person.
        github.user = {"id": 583231, "login": "octo-renamed"}
        later = TestClient(app)
        github_sign_in(later)

        me = later.get(f"{API}/auth/me").json()
        assert me["account"]["party_id"] == account["party_id"]
        assert me["account"]["github_login"] == "octo-renamed"
        contributor = store.get_contributor(account["party_id"])
        assert contributor is not None and contributor.handle == "octo-renamed"

    def test_without_an_oauth_app_github_sign_in_says_what_to_set_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "github_oauth_client_id", "")
        monkeypatch.setattr(settings, "github_oauth_client_secret", "")
        r = TestClient(app).post(f"{API}/auth/github/signin")
        assert r.status_code == 503
        for needed in (
            "MISTHOS_GITHUB_OAUTH_CLIENT_ID", "MISTHOS_GITHUB_OAUTH_CLIENT_SECRET", CALLBACK,
        ):  # fmt: skip
            assert needed in r.json()["detail"]

    def test_a_browser_on_another_address_is_told_where_to_sign_in(
        self, github: FakeGitHub
    ) -> None:
        """The state cookie belongs to the address the browser is on, and GitHub returns
        to the public URL, so a sign-in from 127.0.0.1 could only fail at the end."""
        r = TestClient(app).post(
            f"{API}/auth/github/signin", headers={"Origin": "http://127.0.0.1:5173"}
        )
        assert r.status_code == 409
        assert f"open the app at {PUBLIC} and sign in" in r.json()["detail"]
        assert not any(
            h.startswith("misthos_github_state") for h in r.headers.get_list("set-cookie")
        )


class TestTheSignInIsBoundToItsBrowser:
    """Criterion 2: a callback whose state does not match the browser's cookie is
    refused, and no session is issued (login CSRF)."""

    def test_a_callback_sent_to_another_browser_is_refused_and_signs_nobody_in(
        self, github: FakeGitHub
    ) -> None:
        attacker = TestClient(app)
        state = start_signin(attacker)

        # The attacker approves on GitHub, then sends the callback URL to a victim.
        victim = TestClient(app)
        r = come_back(victim, state)

        assert r.status_code == 403
        assert "not started in this browser" in r.json()["detail"]
        assert not session_cookie_set(r)
        assert victim.get(f"{API}/auth/me").status_code == 401
        assert github.seen == []  # the code was never exchanged
        # And the state is spent, so the attacker cannot finish it either.
        again = come_back(attacker, state)
        assert again.status_code == 400 and "already used" in again.json()["detail"]
        assert not session_cookie_set(again)

    def test_a_cookie_for_another_sign_in_does_not_finish_this_one(
        self, github: FakeGitHub
    ) -> None:
        attacker, victim = TestClient(app), TestClient(app)
        attackers = start_signin(attacker)
        start_signin(victim)  # the victim has a sign-in of their own under way

        r = come_back(victim, attackers)

        assert r.status_code == 403
        assert not session_cookie_set(r)
        assert victim.get(f"{API}/auth/me").status_code == 401

    def test_a_callback_without_the_state_cookie_is_refused(self, github: FakeGitHub) -> None:
        client = TestClient(app)
        state = start_signin(client)
        client.cookies.delete("misthos_github_state")
        r = come_back(client, state)
        assert r.status_code == 403 and not session_cookie_set(r)


class TestTheSimulationsGitHubSignIn:
    def test_a_typed_login_signs_in_as_the_same_made_up_user_every_time(self) -> None:
        first = TestClient(app)
        session = demo_sign_in(first, "Demo-Dev")
        assert session["method"] == "github" and session["account"] is None
        assert session["github_id"] == simulated_user("demo-dev").id
        account = choose(first, "contributor", "x")

        again = demo_sign_in(TestClient(app), "demo-dev")
        assert again["account"]["party_id"] == account["party_id"]

    def test_outside_the_simulation_a_typed_login_signs_nobody_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "simulated", False)
        r = TestClient(app).post(f"{API}/auth/github/simulate-signin", json={"login": "anyone"})
        assert r.status_code == 403
        assert not session_cookie_set(r)

    def test_a_typed_login_cannot_take_over_an_account_linked_before_ids_were_kept(
        self,
    ) -> None:
        """A demo sign-in finds only accounts it made, never one by its login alone."""
        store.create_account("0x" + "ab" * 20, "publisher", "Old Co")
        legacy = store.account_by_address("0x" + "ab" * 20)
        assert legacy is not None
        store.repo.save_account(legacy.model_copy(update={"github_login": "old-co"}))

        client = TestClient(app)
        demo_sign_in(client, "old-co")
        assert client.get(f"{API}/auth/me").json()["account"] is None
        r = client.post(f"{API}/auth/role", json={"role": "publisher", "name": "x"})
        assert r.status_code == 409 and "old-co is linked to another account" in r.json()["detail"]


class TestSigningInWithAWallet:
    """Criterion 3: Sign in with a wallet still works end to end, and that account can
    link GitHub afterwards."""

    def test_a_wallet_signs_in_chooses_a_side_and_links_github(self, github: FakeGitHub) -> None:
        client = TestClient(app)
        wallet = Wallet(131)
        session = wallet_sign_in(client, wallet)
        assert session["method"] == "wallet"
        assert session["address"] == wallet.address.lower() and session["account"] is None
        account = choose(client, "contributor", "ada")
        assert account["address"] == wallet.address.lower()
        assert client.get(f"{API}/auth/me").json()["wallet"]["address"] == wallet.address.lower()

        start = client.post(f"{API}/auth/github/start", headers={"Origin": PUBLIC})
        assert start.status_code == 200, start.text
        state = parse_qs(urlsplit(start.json()["authorize_url"]).query)["state"][0]
        back = come_back(client, state)
        assert back.status_code == 303, back.text
        assert back.headers["location"] == f"{PUBLIC}/account?linked=github"

        me = client.get(f"{API}/auth/me").json()
        assert me["method"] == "wallet"
        assert (me["github_id"], me["github_login"]) == (583231, "octo-dev")
        assert me["account"]["github_id"] == 583231

        # Linked, the same person can now come in with GitHub instead.
        elsewhere = TestClient(app)
        github_sign_in(elsewhere)
        assert elsewhere.get(f"{API}/auth/me").json()["account"]["party_id"] == account["party_id"]
        # And an account linked to one GitHub user is not moved to another.
        again = client.post(f"{API}/auth/github/start", headers={"Origin": PUBLIC})
        assert again.status_code == 409 and "already linked" in again.json()["detail"]

    def test_a_session_from_before_this_change_still_signs_in(self) -> None:
        """Tokens issued before #131 name a bare address; nobody is signed out."""
        client = TestClient(app)
        wallet = Wallet(132)
        wallet_sign_in(client, wallet)
        account = choose(client, "contributor", "old")

        now = datetime.now(UTC)
        old = jwt.encode(
            {"sub": wallet.address.lower(), "iss": "misthos", "iat": now,
             "exp": now + timedelta(hours=12)},
            sessions._SECRET,
            algorithm="HS256",
        )  # fmt: skip
        me = TestClient(app).get(f"{API}/auth/me", headers={"Authorization": f"Bearer {old}"})

        assert me.status_code == 200, me.text
        assert me.json()["method"] == "wallet"
        assert me.json()["account"]["party_id"] == account["party_id"]

    def test_a_token_naming_something_else_is_no_session(self) -> None:
        now = datetime.now(UTC)
        for subject in ("github:not-a-number", "someone", "wallet:0x123"):
            token = jwt.encode(
                {"sub": subject, "iss": "misthos", "iat": now, "exp": now + timedelta(hours=1)},
                sessions._SECRET,
                algorithm="HS256",
            )
            me = TestClient(app).get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert me.status_code == 401, subject


class TestThePublisherConnectsAWalletToFund:
    """Criterion 4: a publisher signed in with GitHub publishes and is priced without a
    wallet; approving the price is refused until one is connected, then it works."""

    def test_the_price_is_approved_only_once_a_wallet_is_connected(self) -> None:
        client = TestClient(app)
        demo_sign_in(client, "acme-gh")
        account = choose(client, "publisher", "Acme GH")
        assert account["address"] is None

        published = client.post(
            f"{API}/issues", json={"repo": "acme/gh", "title": "Retry on 503", "publisher_id": "x"}
        )
        assert published.status_code == 201, published.text
        issue = published.json()
        assert issue["publisher_id"] == account["party_id"]
        assert issue["proposal"]["recommended"]["base_units"] > 0
        iid = issue["id"]
        approved = client.post(
            f"{API}/issues/{iid}/criteria", json={"criteria": issue["acceptance_criteria"]}
        )
        assert approved.status_code == 200, approved.text

        refused = client.post(f"{API}/issues/{iid}/fund")

        assert refused.status_code == 409
        assert "no wallet to fund from: connect one" in refused.json()["detail"]
        rec = store.get(iid)
        assert rec is not None and rec.state is IssueState.AWAITING_APPROVAL
        assert rec.funding is None  # no terms were approved without a wallet

        wallet = Wallet(141)
        connected = connect(client, wallet)
        assert connected.status_code == 200, connected.text
        assert connected.json()["address"] == wallet.address.lower()
        me = client.get(f"{API}/auth/me").json()
        assert me["method"] == "github" and me["address"] == wallet.address.lower()
        assert me["wallet"]["address"] == wallet.address.lower()

        funded = client.post(f"{API}/issues/{iid}/fund")

        assert funded.status_code == 200, funded.text
        assert funded.json()["state"] == "FUNDED"
        rec = store.get(iid)
        assert rec is not None and rec.funding is not None
        assert rec.funding.wallet == wallet.address.lower()  # the escrow names this wallet

    def test_a_publishers_circle_wallet_does_not_fund(self) -> None:
        """#123 keeps the Circle wallet beside the funding wallet, never in its place."""
        client = TestClient(app)
        demo_sign_in(client, "circle-pub")
        account = choose(client, "publisher", "Circle Pub")
        pid = account["party_id"]
        circle = client.post(f"{API}/wallets/publisher/{pid}/session")
        assert circle.status_code == 200 and circle.json()["wallet"] is not None
        assert client.get(f"{API}/auth/me").json()["wallet"] is None

        issue = client.post(
            f"{API}/issues", json={"repo": "circle/pub", "title": "Fix it", "publisher_id": pid}
        ).json()
        client.post(f"{API}/issues/{issue['id']}/criteria",
                    json={"criteria": issue["acceptance_criteria"]})  # fmt: skip
        assert client.post(f"{API}/issues/{issue['id']}/fund").status_code == 409

    def test_a_plan_is_not_bought_without_a_wallet_to_pay_from(self) -> None:
        client = TestClient(app)
        demo_sign_in(client, "plan-pub")
        pid = choose(client, "publisher", "Plan Pub")["party_id"]
        r = client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"})
        assert r.status_code == 409 and "no wallet to pay for Team from" in r.json()["detail"]
        assert connect(client, Wallet(142)).status_code == 200
        r = client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"})
        assert r.status_code == 200
        assert r.json()["pending"]["payer"] == Wallet(142).address.lower()


class TestTheContributorConnectsAWalletToClaim:
    """Criterion 5: a contributor signed in with GitHub cannot claim until a wallet or a
    Circle wallet is connected, and then can."""

    def test_a_claim_waits_for_a_connected_wallet(self) -> None:
        client = TestClient(app)
        demo_sign_in(client, "claim-dev")
        choose(client, "contributor", "x")

        refused = client.post(f"{API}/issues/ISS-1001/claim")

        assert refused.status_code == 409
        assert "no wallet to be paid to" in refused.json()["detail"]
        assert store.get("ISS-1001").state is IssueState.FUNDED  # type: ignore[union-attr]

        assert connect(client, Wallet(151)).status_code == 200
        claimed = client.post(f"{API}/issues/ISS-1001/claim")
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["state"] == "CLAIMED"

    def test_a_circle_wallet_is_enough_to_claim(self) -> None:
        client = TestClient(app)
        demo_sign_in(client, "circle-dev")
        pid = choose(client, "contributor", "x")["party_id"]
        assert client.post(f"{API}/issues/ISS-1001/claim").status_code == 409

        circle = client.post(f"{API}/wallets/contributor/{pid}/session")
        assert circle.status_code == 200 and circle.json()["wallet"] is not None
        me = client.get(f"{API}/auth/me").json()
        assert me["address"] is None  # nothing connected by signature
        assert me["wallet"]["address"] == circle.json()["wallet"]["address"]

        claimed = client.post(f"{API}/issues/ISS-1001/claim")
        assert claimed.status_code == 200 and claimed.json()["state"] == "CLAIMED"


class TestOneWalletOneAccount:
    """Criterion 6: connecting a wallet another account has is a 409; connecting the
    wallet of a wallet-only account from before this change joins it."""

    def test_a_wallet_another_account_has_is_refused(self) -> None:
        owner = TestClient(app)
        wallet = Wallet(161)
        wallet_sign_in(owner, wallet)
        choose(owner, "contributor", "owner")
        owner.post(f"{API}/auth/github/simulate", json={"login": "owner-dev"})

        other = TestClient(app)
        demo_sign_in(other, "other-dev")
        mine = choose(other, "contributor", "x")

        r = connect(other, wallet)

        assert r.status_code == 409 and "belongs to another account" in r.json()["detail"]
        after = store.account(mine["party_id"])
        assert after is not None and after.address is None

    def test_a_wallet_only_account_is_not_joined_to_someone_who_has_an_account(self) -> None:
        legacy = TestClient(app)
        wallet = Wallet(162)
        wallet_sign_in(legacy, wallet)
        choose(legacy, "contributor", "legacy")

        other = TestClient(app)
        demo_sign_in(other, "has-one")
        choose(other, "publisher", "Has One")
        assert connect(other, wallet).status_code == 409

    def test_the_wallet_of_a_wallet_only_account_joins_it_to_the_github_user(self) -> None:
        legacy = TestClient(app)
        wallet = Wallet(163)
        wallet_sign_in(legacy, wallet)
        old = choose(legacy, "contributor", "legacy-name")
        # Its history: a claim it holds.
        legacy.post(f"{API}/auth/github/simulate", json={"login": "temp-link"})
        assert legacy.post(f"{API}/issues/ISS-1001/claim").json()["state"] == "CLAIMED"
        # Unlinked again, as every wallet-only account from before #131 is.
        unlinked = store.account(old["party_id"])
        assert unlinked is not None
        store.repo.save_account(
            unlinked.model_copy(update={"github_id": None, "github_login": None})
        )

        github_user = TestClient(app)
        demo_sign_in(github_user, "came-back")
        joined = connect(github_user, wallet)

        assert joined.status_code == 200, joined.text
        assert joined.json()["party_id"] == old["party_id"]
        assert joined.json()["github_id"] == simulated_user("came-back").id
        assert joined.json()["github_login"] == "came-back"
        me = github_user.get(f"{API}/auth/me").json()
        assert me["method"] == "github" and me["account"]["party_id"] == old["party_id"]
        assert store.get("ISS-1001").contributor_id == old["party_id"]  # type: ignore[union-attr]
        contributor = store.get_contributor(old["party_id"])
        assert contributor is not None and contributor.handle == "came-back"
        # The wallet still signs in to it too.
        assert wallet_sign_in(TestClient(app), wallet)["account"]["party_id"] == old["party_id"]

    def test_before_choosing_a_side_a_new_wallet_waits_for_one(self) -> None:
        client = TestClient(app)
        demo_sign_in(client, "no-side")
        r = connect(client, Wallet(164))
        assert r.status_code == 409 and "choose a side first" in r.json()["detail"]

    def test_an_account_keeps_the_wallet_it_connected(self) -> None:
        client = TestClient(app)
        demo_sign_in(client, "keeps-one")
        choose(client, "contributor", "x")
        assert connect(client, Wallet(165)).status_code == 200
        assert connect(client, Wallet(165)).status_code == 200  # the same again is fine
        r = connect(client, Wallet(166))
        assert r.status_code == 409 and "a wallet is connected once" in r.json()["detail"]

    def test_a_connect_spends_its_nonce_and_proves_the_wallet(self) -> None:
        client = TestClient(app)
        demo_sign_in(client, "nonce-dev")
        choose(client, "contributor", "x")
        body = signed(client, Wallet(167))
        tampered = {**body, "signature": Wallet(168).sign(body["message"])}
        assert client.post(f"{API}/auth/wallet/connect", json=tampered).status_code == 401
        # Refused, and spent: the right signature over the same nonce is refused too.
        assert client.post(f"{API}/auth/wallet/connect", json=body).status_code == 401
        assert store.account_by_address(Wallet(167).address) is None

    def test_two_requests_spending_one_nonce_at_once_spend_it_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reading the nonce and then deleting it let both of two requests that arrived
        together read it. A slow read lines them up every time."""
        store.coordinator.put("siwe:race", "issued", auth_routes.NONCE_TTL)
        read = store.coordinator.get
        both_read = threading.Barrier(2)

        def slow(key: str) -> str | None:
            found = read(key)
            both_read.wait(timeout=5)
            return found

        monkeypatch.setattr(store.coordinator, "get", slow)
        with ThreadPoolExecutor(2) as pool:
            taken = list(pool.map(lambda _: auth_routes._take("siwe:race"), range(2)))
        monkeypatch.undo()
        assert sorted(taken, key=str) == [None, "issued"]
        assert store.coordinator.get("siwe:race") is None

    def test_connecting_needs_a_session(self) -> None:
        client = TestClient(app)
        r = client.post(f"{API}/auth/wallet/connect", json=signed(client, Wallet(169)))
        assert r.status_code == 401


class TestAccountsLinkedBeforeIds:
    """An account linked to GitHub before #131 has a login and no id. Its first GitHub
    sign-in finds it by the login and fills the id in; after that only the id counts."""

    def test_the_first_github_sign_in_fills_in_the_id(self, every_store: Store) -> None:
        old = every_store.create_account("0x" + "cd" * 20, "contributor", "pre")
        every_store.repo.save_account(old.model_copy(update={"github_login": "Pre-Dev"}))

        found = every_store.sign_in_with_github(777, "pre-dev")

        assert found is not None and found.party_id == old.party_id
        assert found.github_id == 777
        again = every_store.account_by_github_id(777)
        assert again is not None and again.party_id == old.party_id

    def test_once_an_account_has_an_id_its_login_alone_finds_nothing(
        self, every_store: Store
    ) -> None:
        """A renamed login can be taken by someone else, who must not inherit it."""
        every_store.create_account(
            None, "contributor", "x", github_id=1001, github_login="taken-name"
        )
        assert every_store.sign_in_with_github(2002, "taken-name") is None

    def test_wallets_and_github_ids_are_one_account_each_in_every_store(
        self, every_store: Store
    ) -> None:
        from misthos.repositories import AccountConflict

        first = every_store.create_account(
            "0x" + "ee" * 20, "publisher", "One", github_id=5, github_login="one"
        )
        second = every_store.create_account(None, "publisher", "Two", github_id=6)
        for clash in (
            {"address": "0x" + "EE" * 20},
            {"github_id": 5},
            {"github_login": "ONE"},
        ):
            with pytest.raises(AccountConflict):
                every_store.repo.save_account(second.model_copy(update=clash))
        assert every_store.repo.get_account_by_address("0x" + "EE" * 20) == first
        assert every_store.repo.get_account(second.party_id) == second


def deliver(event: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    r = TestClient(app).post(
        f"{API}/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": signature_for(body, settings.github_webhook_secret),
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 202, r.text


class TestFromGitHubAlone:
    """`/misthos approve` on the issue says which wallet is missing too, and where."""

    def test_a_publisher_without_a_wallet_is_told_to_connect_one(self) -> None:
        github = store.github
        assert isinstance(github, SimulatedGitHub)
        client = TestClient(app)
        demo_sign_in(client, "gh-maint")
        choose(client, "publisher", "GH Maint")
        repo = "gh/widgets"
        deliver("installation", {
            "action": "created", "installation": {"id": 7}, "sender": {"login": "gh-maint"},
            "repositories": [{"full_name": repo}],
        })  # fmt: skip
        deliver("issues", {
            "action": "labeled", "repository": {"full_name": repo}, "label": {"name": "misthos"},
            "issue": {"number": 21, "title": "Retry on 503", "labels": [{"name": "misthos"}]},
        })  # fmt: skip
        rec = store.find_open(repo, 21)
        assert rec is not None

        def approve() -> str:
            deliver("issue_comment", {
                "action": "created", "repository": {"full_name": repo}, "issue": {"number": 21},
                "comment": {"body": "/misthos approve", "user": {"login": "gh-maint"}},
                "sender": {"login": "gh-maint"},
            })  # fmt: skip
            return [m.body for m in github.sent if m.kind == "comment" and m.target == "21"][-1]

        reply = approve()

        assert reply.startswith("@gh-maint, refused: GH Maint has no wallet to fund from")
        assert f"{PUBLIC}/account" in reply
        assert store.get(rec.id).state is IssueState.AWAITING_APPROVAL  # type: ignore[union-attr]

        assert connect(client, Wallet(171)).status_code == 200
        approve()
        assert store.get(rec.id).state is IssueState.FUNDED  # type: ignore[union-attr]


class TestNothingSecretIsLogged:
    """GitHub's return carries a one-time code and the state in its URL, which uvicorn's
    access line writes out whole."""

    @staticmethod
    def access_line(path: str) -> str:
        root, access = logging.getLogger(), logging.getLogger("uvicorn.access")
        saved = (root.handlers[:], root.level, access.handlers[:], access.propagate)
        access.handlers, access.propagate = [logging.NullHandler()], False
        try:
            logs.configure(json_lines=True)
            out = io.StringIO()
            handler = root.handlers[0]
            assert isinstance(handler, logging.StreamHandler)
            handler.setStream(out)
            access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET", path, "1.1", 303)
            return json.loads(out.getvalue())["message"]
        finally:
            root.handlers, access.handlers, access.propagate = saved[0], saved[2], saved[3]
            root.setLevel(saved[1])

    def test_the_callbacks_code_and_state_never_reach_the_log(self) -> None:
        line = self.access_line("/api/v1/auth/github/callback?code=c0de-1234&state=st4te-5678")
        assert "c0de-1234" not in line and "st4te-5678" not in line
        assert "/api/v1/auth/github/callback?code=[redacted]&state=[redacted]" in line

    def test_other_query_strings_are_logged_as_they_were(self) -> None:
        assert "/api/v1/issues?state=FUNDED" in self.access_line("/api/v1/issues?state=FUNDED")

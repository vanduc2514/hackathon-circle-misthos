"""Single sign-on for an Enterprise organisation (#53).

Staff sign in through the organisation's own identity provider and act as its account;
the organisation can make that the only way in, and taking the connection away ends
every session it issued. Against a fake OpenID Connect provider on
`httpx.MockTransport` that checks what a real one checks: the client's secret, the
redirect URI and the PKCE verifier, and signs its ID tokens with a key it publishes.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from misthos.api.v1 import auth as auth_routes
from misthos.config import settings
from misthos.domain.issue import IssueState
from misthos.domain.signals import IssueFacts
from misthos.domain.sso import SsoTerms
from misthos.main import app
from misthos.repositories import MemoryRepository, SsoDomainTaken
from misthos.repositories.sql import SqlRepository
from misthos.services.github import SimulatedGitHub
from misthos.services.sso import OidcClient
from misthos.store import Store, store

API = "/api/v1"
PUBLIC = "http://localhost:5173"
CALLBACK = f"{PUBLIC}/api/v1/auth/sso/callback"
ISSUER = "https://idp.acme.example"
CONNECTION = {
    "issuer": ISSUER,
    "client_id": "misthos-acme",
    "client_secret_ref": "acme-oidc",
    "domains": ["acme.example"],
}


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


class FakeProvider:
    """An identity provider for one person at a time. It answers discovery, publishes
    its key, and exchanges a code only for the client's secret, the registered redirect
    URI and the verifier whose hash the authorize request carried."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.claims: dict = {
            "sub": "00u-dana", "email": "dana@acme.example", "email_verified": True,
        }  # fmt: skip
        self.challenge = ""
        self.nonce = ""
        self.issuer = ISSUER
        self.audience = "misthos-acme"
        self.discovered_issuer = ISSUER

    def authorized(self, authorize_url: str) -> str:
        """The person approves at the provider; the state it was sent with."""
        parts = urlsplit(authorize_url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{ISSUER}/authorize"
        assert query["client_id"] == "misthos-acme" and query["redirect_uri"] == CALLBACK
        assert query["response_type"] == "code" and query["code_challenge_method"] == "S256"
        assert "openid" in query["scope"].split()
        self.challenge, self.nonce = query["code_challenge"], query["nonce"]
        return query["state"]

    def id_token(self) -> str:
        now = datetime.now(UTC)
        claims = {
            "iss": self.issuer, "aud": self.audience, "iat": now,
            "exp": now + timedelta(minutes=5), "nonce": self.nonce, **self.claims,
        }  # fmt: skip
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.discovered_issuer,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/keys",
                },
            )
        if path == "/keys":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "k1", "use": "sig"}]})
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            proven = base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == self.challenge
            if (
                form.get("client_secret") != "s3cret"
                or form.get("redirect_uri") != CALLBACK
                or form.get("code") != "c0de"
                or not proven
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.id_token(), "access_token": "x"})
        return httpx.Response(404)


class Secrets:
    """The secret store, holding what onboarding put there."""

    def __init__(self) -> None:
        self.held = {"acme-oidc": "s3cret"}

    def read(self, reference: str) -> str | None:
        return self.held.get(reference)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    fake = FakeProvider()
    monkeypatch.setattr(
        auth_routes, "OidcClient", partial(OidcClient, transport=httpx.MockTransport(fake))
    )
    secrets = Secrets()
    monkeypatch.setattr(auth_routes, "secret_store", lambda _directory: secrets)
    return fake


def owner(client: TestClient, login: str = "acme-bot", name: str = "Acme") -> str:
    """The organisation's owner signs in with GitHub and makes it a publisher."""
    r = client.post(f"{API}/auth/github/simulate-signin", json={"login": login})
    assert r.status_code == 200, r.text
    r = client.post(f"{API}/auth/role", json={"role": "publisher", "name": name})
    assert r.status_code == 201, r.text
    return r.json()["party_id"]


def enterprise(client: TestClient, **kw: object) -> str:
    pid = owner(client, **kw)  # type: ignore[arg-type]
    store.set_contract_plan(pid, "enterprise", datetime.now(UTC) + timedelta(days=365))
    return pid


def configure(client: TestClient, pid: str, **changes: object) -> httpx.Response:
    return client.put(f"{API}/publishers/{pid}/sso", json={**CONNECTION, **changes})


def sso_sign_in(client: TestClient, provider: FakeProvider, email: str = "dana@acme.example"):
    r = client.post(f"{API}/auth/sso/signin", json={"email": email}, headers={"Origin": PUBLIC})
    assert r.status_code == 200, r.text
    state = provider.authorized(r.json()["authorize_url"])
    return come_back(client, state)


def come_back(client: TestClient, state: str, **params: str) -> httpx.Response:
    return client.get(
        f"{API}/auth/sso/callback",
        params={"code": "c0de", "state": state, **params},
        follow_redirects=False,
    )


class TestSettingItUp:
    def test_single_sign_on_is_in_the_enterprise_plan_only(self) -> None:
        client = TestClient(app)
        pid = owner(client)
        r = configure(client, pid)
        assert r.status_code == 402 and "Enterprise" in r.json()["detail"]
        assert client.get(f"{API}/publishers/{pid}/sso").json() == {
            "connection": None,
            "callback_url": CALLBACK,
            "entitled": False,
        }

    def test_an_enterprise_organisation_connects_its_provider(self) -> None:
        client = TestClient(app)
        pid = enterprise(client)
        r = configure(client, pid, domains=["@Acme.Example", "acme-labs.example"])
        assert r.status_code == 200, r.text
        connection = r.json()["connection"]
        assert connection["domains"] == ["acme-labs.example", "acme.example"]
        assert connection["client_secret_ref"] == "acme-oidc" and not connection["required"]
        assert r.json()["entitled"] and r.json()["callback_url"] == CALLBACK

    @pytest.mark.parametrize(
        ("change", "said"),
        [
            ({"issuer": "http://idp.acme.example"}, "https"),
            ({"domains": ["gmail.com"]}, "anyone can have an address"),
            ({"domains": ["not a domain"]}, "not an e-mail domain"),
            ({"client_secret_ref": "../../etc/passwd"}, "by its reference"),
        ],
    )
    def test_a_connection_that_cannot_work_safely_is_refused(
        self, change: dict, said: str
    ) -> None:
        client = TestClient(app)
        pid = enterprise(client)
        r = configure(client, pid, **change)
        assert r.status_code == 422 and said in r.json()["detail"]

    def test_a_domain_signs_in_to_one_organisation_only(self) -> None:
        acme, rival = TestClient(app), TestClient(app)
        assert configure(acme, enterprise(acme)).status_code == 200
        r = configure(rival, enterprise(rival, login="rival-bot", name="Rival"))
        assert r.status_code == 409 and "acme.example" in r.json()["detail"]

    def test_outside_the_simulation_the_provider_must_answer_first(
        self, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = TestClient(app)
        pid = enterprise(client)
        monkeypatch.setattr(settings, "simulated", False)

        r = configure(client, pid, client_secret_ref="not-there")
        assert r.status_code == 422 and "holds nothing at not-there" in r.json()["detail"]
        provider.discovered_issuer = "https://evil.example"
        r = configure(client, pid)
        assert r.status_code == 422 and "names another issuer" in r.json()["detail"]
        provider.discovered_issuer = ISSUER
        assert configure(client, pid).status_code == 200


class TestSigningIn:
    def test_staff_sign_in_through_the_provider_and_act_as_the_organisation(
        self, provider: FakeProvider
    ) -> None:
        setup, staff = TestClient(app), TestClient(app)
        pid = enterprise(setup)
        configure(setup, pid)

        back = sso_sign_in(staff, provider)
        assert back.status_code == 303, back.text
        assert back.headers["location"] == f"{PUBLIC}/account?signed_in=sso"
        me = staff.get(f"{API}/auth/me").json()
        assert me["method"] == "sso" and me["sso_email"] == "dana@acme.example"
        assert me["account"]["party_id"] == pid and not me["sso_required"]

    def test_the_decision_log_names_the_person_not_the_organisation(
        self, provider: FakeProvider
    ) -> None:
        setup, staff = TestClient(app), TestClient(app)
        pid = enterprise(setup)
        configure(setup, pid)
        sso_sign_in(staff, provider)
        github = store.github
        assert isinstance(github, SimulatedGitHub)
        github.put_issue(IssueFacts(repo="acme/widgets", number=5, title="Retry", body="x"))

        issue = staff.post(
            f"{API}/issues",
            json={"repo": "acme/widgets", "number": 5, "title": "x", "publisher_id": pid},
        ).json()
        assert issue["publisher_id"] == pid
        criteria = ["A test reproduces the 503 from the settlement API."]
        r = staff.post(f"{API}/issues/{issue['id']}/criteria", json={"criteria": criteria})
        assert r.status_code == 200, r.text
        timeline = staff.get(f"{API}/issues/{issue['id']}/timeline").json()
        outcomes = [e["outcome"] for e in timeline]
        assert "dana@acme.example approved 1 acceptance criteria" in outcomes

    def test_a_release_is_approved_by_a_named_person_never_through_the_sign_on(self) -> None:
        setup, staff = TestClient(app), TestClient(app)
        configure(setup, enterprise(setup))
        staff.post(f"{API}/auth/sso/simulate-signin", json={"email": "dana@acme.example"})
        r = staff.post(f"{API}/issues/ISS-1006/approve-release")
        assert r.status_code == 403 and "not through its single sign-on" in r.json()["detail"]

    def test_a_sign_in_this_browser_did_not_start_issues_no_session(
        self, provider: FakeProvider
    ) -> None:
        setup, attacker, victim = TestClient(app), TestClient(app), TestClient(app)
        configure(setup, pid := enterprise(setup))
        r = attacker.post(f"{API}/auth/sso/signin", json={"email": "eve@acme.example"})
        state = provider.authorized(r.json()["authorize_url"])

        back = come_back(victim, state)
        assert back.status_code == 403 and "not started in this browser" in back.json()["detail"]
        assert victim.get(f"{API}/auth/me").status_code == 401
        assert pid  # the organisation exists; only the session was refused

    def test_a_state_signs_in_once(self, provider: FakeProvider) -> None:
        setup, staff = TestClient(app), TestClient(app)
        configure(setup, enterprise(setup))
        r = staff.post(f"{API}/auth/sso/signin", json={"email": "dana@acme.example"})
        state = provider.authorized(r.json()["authorize_url"])
        assert come_back(staff, state).status_code == 303
        again = come_back(staff, state)
        assert again.status_code == 400 and "unknown, used or expired" in again.json()["detail"]

    @pytest.mark.parametrize(
        ("claims", "said"),
        [
            ({"email": "dana@elsewhere.example"}, "not in a domain"),
            ({"email_verified": False}, "has not verified"),
            ({"email": None}, "did not say who you are"),
        ],
    )
    def test_an_address_the_connection_does_not_admit_is_refused(
        self, provider: FakeProvider, claims: dict, said: str
    ) -> None:
        setup, staff = TestClient(app), TestClient(app)
        configure(setup, enterprise(setup))
        provider.claims.update(claims)
        back = sso_sign_in(staff, provider)
        assert back.status_code == 403 and said in back.json()["detail"]
        assert staff.get(f"{API}/auth/me").status_code == 401

    @pytest.mark.parametrize(
        ("attribute", "value"),
        [("audience", "another-client"), ("issuer", "https://evil.example"), ("nonce", "replay")],
    )
    def test_an_id_token_not_minted_for_this_sign_in_is_refused(
        self, provider: FakeProvider, attribute: str, value: str
    ) -> None:
        setup, staff = TestClient(app), TestClient(app)
        configure(setup, enterprise(setup))
        r = staff.post(f"{API}/auth/sso/signin", json={"email": "dana@acme.example"})
        state = provider.authorized(r.json()["authorize_url"])
        setattr(provider, attribute, value)
        back = come_back(staff, state)
        assert back.status_code == 502
        assert staff.get(f"{API}/auth/me").status_code == 401

    def test_a_refusal_at_the_provider_is_said_and_signs_nobody_in(
        self, provider: FakeProvider
    ) -> None:
        setup, staff = TestClient(app), TestClient(app)
        configure(setup, enterprise(setup))
        r = staff.post(f"{API}/auth/sso/signin", json={"email": "dana@acme.example"})
        state = provider.authorized(r.json()["authorize_url"])
        back = come_back(staff, state, error="access_denied")
        assert back.status_code == 403 and back.json()["detail"].endswith(": access_denied")

    def test_an_unknown_domain_is_told_there_is_no_sign_on_there(self) -> None:
        r = TestClient(app).post(f"{API}/auth/sso/signin", json={"email": "x@nowhere.example"})
        assert r.status_code == 404 and "nowhere.example" in r.json()["detail"]

    def test_the_demo_signs_in_with_a_typed_work_email(self) -> None:
        setup, staff = TestClient(app), TestClient(app)
        configure(setup, pid := enterprise(setup))
        r = staff.post(f"{API}/auth/sso/simulate-signin", json={"email": "Dana@Acme.example"})
        assert r.status_code == 200, r.text
        assert r.json()["sso_email"] == "dana@acme.example"
        assert r.json()["account"]["party_id"] == pid
        outside = staff.post(f"{API}/auth/sso/simulate-signin", json={"email": "x@other.example"})
        assert outside.status_code == 404

    def test_single_sign_on_never_makes_an_account_of_its_own(self) -> None:
        setup, staff = TestClient(app), TestClient(app)
        configure(setup, enterprise(setup))
        staff.post(f"{API}/auth/sso/simulate-signin", json={"email": "dana@acme.example"})
        r = staff.post(f"{API}/auth/role", json={"role": "contributor", "name": "dana"})
        assert r.status_code == 409


class TestRequiringIt:
    def test_requiring_it_takes_a_session_that_came_through_it(self) -> None:
        setup, staff = TestClient(app), TestClient(app)
        pid = enterprise(setup)
        configure(setup, pid)
        r = configure(setup, pid, required=True)
        assert r.status_code == 403 and "lock the organisation out" in r.json()["detail"]

        staff.post(f"{API}/auth/sso/simulate-signin", json={"email": "dana@acme.example"})
        r = configure(staff, pid, required=True)
        assert r.status_code == 200 and r.json()["connection"]["required"]

    def test_once_required_no_other_session_acts_for_the_organisation(self) -> None:
        setup, staff = TestClient(app), TestClient(app)
        pid = enterprise(setup)
        configure(setup, pid)
        staff.post(f"{API}/auth/sso/simulate-signin", json={"email": "dana@acme.example"})
        configure(staff, pid, required=True)

        r = setup.get(f"{API}/publishers/{pid}/sso")
        assert r.status_code == 403 and "single sign-on" in r.json()["detail"]
        assert setup.put(f"{API}/publishers/{pid}/policy", json={}).status_code == 403
        assert setup.get(f"{API}/auth/me").json()["sso_required"] is True
        # The public pages stay public; only the organisation's own records are shut.
        assert setup.get(f"{API}/publishers").status_code == 200
        # Through the sign-on, everything still works.
        assert staff.get(f"{API}/publishers/{pid}/sso").status_code == 200

    def test_once_required_a_comment_from_the_organisations_login_commits_nothing(
        self,
    ) -> None:
        from test_github_native_loop import REPO, commented, comments, installed, labelled

        setup, staff = TestClient(app), TestClient(app)
        pid = enterprise(setup)
        configure(setup, pid)
        staff.post(f"{API}/auth/sso/simulate-signin", json={"email": "dana@acme.example"})
        configure(staff, pid, required=True)
        github = store.github
        assert isinstance(github, SimulatedGitHub)
        github.put_issue(IssueFacts(repo=REPO, number=12, title="Retry on 503",
                                    body="`src/retry.py` gives up on the first 503.",
                                    labels=("bug", "misthos")))  # fmt: skip
        installed("acme-bot")
        labelled(12)
        rec = store.find_open(REPO, 12)
        assert rec is not None and rec.publisher_id == pid

        commented(12, "acme-bot", "/misthos approve")
        assert store.get(rec.id).state is IssueState.AWAITING_APPROVAL  # type: ignore[union-attr]
        assert "acts only through its single sign-on" in comments(github, 12)[-1]

    def test_a_requirement_survives_a_lapsed_plan(self) -> None:
        setup, staff = TestClient(app), TestClient(app)
        pid = enterprise(setup)
        configure(setup, pid)
        staff.post(f"{API}/auth/sso/simulate-signin", json={"email": "dana@acme.example"})
        configure(staff, pid, required=True)
        store.set_contract_plan(pid, "open", datetime.now(UTC) + timedelta(days=1))

        assert setup.get(f"{API}/publishers/{pid}/sso").status_code == 403
        assert staff.get(f"{API}/publishers/{pid}/sso").json()["connection"]["required"]

    def test_removing_the_connection_ends_every_session_it_issued(self) -> None:
        setup, staff = TestClient(app), TestClient(app)
        pid = enterprise(setup)
        configure(setup, pid)
        staff.post(f"{API}/auth/sso/simulate-signin", json={"email": "dana@acme.example"})
        configure(staff, pid, required=True)

        assert staff.delete(f"{API}/publishers/{pid}/sso").status_code == 204
        assert staff.get(f"{API}/auth/me").json()["account"] is None
        # And the owner's own session acts again.
        assert setup.get(f"{API}/publishers/{pid}/sso").json()["connection"] is None

    def test_dropping_a_domain_ends_the_sessions_from_it(self) -> None:
        setup, staff = TestClient(app), TestClient(app)
        pid = enterprise(setup)
        configure(setup, pid, domains=["acme.example", "acme-labs.example"])
        staff.post(f"{API}/auth/sso/simulate-signin", json={"email": "lee@acme-labs.example"})
        assert staff.get(f"{API}/auth/me").json()["account"]["party_id"] == pid

        configure(setup, pid)
        assert staff.get(f"{API}/auth/me").json()["account"] is None


@pytest.fixture(params=["memory", "sqlite"])
def repo_store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    repo = (
        MemoryRepository()
        if request.param == "memory"
        else SqlRepository(f"sqlite:///{tmp_path / 'misthos.db'}")
    )
    fresh = Store(repo)
    fresh.reset()
    return fresh


class TestKeepingIt:
    def test_a_connection_is_found_by_its_organisation_and_by_any_of_its_domains(
        self, repo_store: Store
    ) -> None:
        terms = SsoTerms(ISSUER, "misthos-acme", "acme-oidc", ("b.example", "a.example"))
        kept = repo_store.set_sso("PUB-1", terms)
        assert kept.domains == ["a.example", "b.example"]
        assert repo_store.sso("PUB-1") == kept
        assert repo_store.sso_for_email("Dana@B.Example") == kept
        assert repo_store.sso_for_email("dana@c.example") is None

        repo_store.set_sso("PUB-1", SsoTerms(ISSUER, "misthos-acme", "acme-oidc", ("c.example",)))
        assert repo_store.sso_for_email("dana@a.example") is None
        assert repo_store.sso_for_email("dana@c.example") is not None

    def test_a_domain_another_organisation_holds_is_refused_and_nothing_is_written(
        self, repo_store: Store
    ) -> None:
        repo_store.set_sso("PUB-1", SsoTerms(ISSUER, "a", "acme-oidc", ("a.example",)))
        with pytest.raises(SsoDomainTaken):
            repo_store.set_sso("PUB-2", SsoTerms(ISSUER, "b", "b-oidc", ("z.example", "a.example")))
        assert repo_store.sso("PUB-2") is None
        assert repo_store.sso_for_email("x@z.example") is None

    def test_removing_a_connection_frees_its_domains(self, repo_store: Store) -> None:
        repo_store.set_sso("PUB-1", SsoTerms(ISSUER, "a", "acme-oidc", ("a.example",)))
        repo_store.remove_sso("PUB-1")
        assert repo_store.sso("PUB-1") is None
        repo_store.set_sso("PUB-2", SsoTerms(ISSUER, "b", "b-oidc", ("a.example",)))
        assert repo_store.sso_for_email("x@a.example").publisher_id == "PUB-2"  # type: ignore[union-attr]

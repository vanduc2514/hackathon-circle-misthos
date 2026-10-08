"""Sign-in with a wallet (#70), a linked GitHub account (#80), approved criteria
before funding (#21), and the explicit actions that take an issue from publication
to payment without the demo stepper (#71)."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from siwe_wallet import Wallet

from misthos.api.v1 import auth as auth_routes
from misthos.config import settings
from misthos.domain.issue import IssueState
from misthos.domain.review import ChangedFile
from misthos.domain.signals import IssueFacts
from misthos.main import app
from misthos.services.github import PullRequest, SimulatedGitHub
from misthos.services.github.oauth import GitHubOAuth
from misthos.store import store

API = "/api/v1"
REPO = "acme/widgets"


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


@pytest.fixture
def github() -> SimulatedGitHub:
    assert isinstance(store.github, SimulatedGitHub)
    return store.github


def sign_in(client: TestClient, wallet: Wallet) -> dict:
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    r = client.post(
        f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)}
    )
    assert r.status_code == 200, r.text
    return r.json()


def onboard(client: TestClient, wallet: Wallet, role: str, name: str, login: str) -> dict:
    sign_in(client, wallet)
    account = client.post(f"{API}/auth/role", json={"role": role, "name": name})
    assert account.status_code == 201, account.text
    linked = client.post(f"{API}/auth/github/simulate", json={"login": login})
    assert linked.status_code == 200, linked.text
    return linked.json()


class TestSignIn:
    def test_a_wallet_signs_in_and_chooses_a_role_once(self) -> None:
        client = TestClient(app)
        wallet = Wallet(11)
        session = sign_in(client, wallet)
        assert session["address"] == wallet.address.lower()
        assert session["account"] is None
        assert client.get(f"{API}/auth/me").json()["account"] is None

        r = client.post(f"{API}/auth/role", json={"role": "contributor", "name": "ada"})
        assert r.status_code == 201 and r.json()["party_id"].startswith("CON-")
        again = client.post(f"{API}/auth/role", json={"role": "publisher", "name": "Ada Inc"})
        assert again.status_code == 409
        assert client.get(f"{API}/auth/me").json()["account"]["role"] == "contributor"

    def test_a_nonce_signs_in_once(self) -> None:
        client = TestClient(app)
        wallet = Wallet(12)
        nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
        message = wallet.message(nonce)
        body = {"message": message, "signature": wallet.sign(message)}
        assert client.post(f"{API}/auth/verify", json=body).status_code == 200
        assert client.post(f"{API}/auth/verify", json=body).status_code == 401

    def test_a_message_for_another_site_is_refused(self) -> None:
        client = TestClient(app)
        wallet = Wallet(13)
        nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
        message = wallet.message(nonce, domain="evil.example")
        r = client.post(
            f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)}
        )
        assert r.status_code == 401

    def test_a_bearer_token_works_like_the_cookie(self) -> None:
        token = sign_in(TestClient(app), Wallet(14))["token"]
        fresh = TestClient(app)
        assert fresh.get(f"{API}/auth/me").status_code == 401
        me = fresh.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200

    def test_signing_out_ends_the_session(self) -> None:
        client = TestClient(app)
        sign_in(client, Wallet(15))
        client.post(f"{API}/auth/logout")
        assert client.get(f"{API}/auth/me").status_code == 401


class TestGitHubLink:
    def test_linking_goes_through_github_and_keeps_only_the_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[httpx.Request] = []

        def github(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/login/oauth/access_token":
                return httpx.Response(200, json={"access_token": "gho_once"})
            return httpx.Response(200, json={"login": "ada-dev"})

        client_ = GitHubOAuth("client-1", "secret-1", transport=httpx.MockTransport(github))
        monkeypatch.setattr(auth_routes, "oauth", lambda: client_)

        client = TestClient(app)
        sign_in(client, Wallet(21))
        client.post(f"{API}/auth/role", json={"role": "contributor", "name": "ada"})
        start = client.post(f"{API}/auth/github/start").json()["authorize_url"]
        state = parse_qs(urlsplit(start).query)["state"][0]
        assert parse_qs(urlsplit(start).query)["client_id"] == ["client-1"]

        r = client.get(
            f"{API}/auth/github/callback", params={"code": "c0de", "state": state},
            follow_redirects=False,
        )  # fmt: skip
        assert r.status_code == 303
        account = client.get(f"{API}/auth/me").json()["account"]
        assert account["github_login"] == "ada-dev"
        contributor = store.get_contributor(account["party_id"])
        assert contributor is not None and contributor.handle == "ada-dev"
        assert seen[-1].headers["Authorization"] == "Bearer gho_once"
        # The state is spent.
        replay = client.get(f"{API}/auth/github/callback", params={"code": "c0de", "state": state})
        assert replay.status_code == 400

    def test_one_github_login_belongs_to_one_wallet(self) -> None:
        first, second = TestClient(app), TestClient(app)
        onboard(first, Wallet(22), "contributor", "a", "same-login")
        sign_in(second, Wallet(23))
        second.post(f"{API}/auth/role", json={"role": "contributor", "name": "b"})
        r = second.post(f"{API}/auth/github/simulate", json={"login": "same-login"})
        assert r.status_code == 409


class TestTheWholeLoopWithoutTheDemoStepper:
    def test_publication_to_payment_through_explicit_actions(self, github: SimulatedGitHub) -> None:
        publisher, contributor = TestClient(app), TestClient(app)
        onboard(publisher, Wallet(31), "publisher", "Acme", "acme-bot")
        alice = onboard(contributor, Wallet(32), "contributor", "alice", "alice-dev")

        github.put_issue(
            IssueFacts(repo=REPO, number=5, title="Retry on 503", body="Fix `src/retry.py`.")
        )
        published = publisher.post(
            f"{API}/issues",
            json={"repo": REPO, "number": 5, "title": "x", "publisher_id": "PUB-1"},
        )
        assert published.status_code == 201, published.text
        issue = published.json()
        assert issue["publisher_id"] != "PUB-1"  # a signed-in publisher publishes as itself
        iid = issue["id"]

        # No funding before the criteria are approved (#21).
        assert publisher.post(f"{API}/issues/{iid}/fund").status_code == 409
        criteria = [
            "A test reproduces the 503 from the settlement API.",
            "Retries back off exponentially, starting at 100 ms.",
            "The changelog records the change.",
        ]
        r = publisher.post(f"{API}/issues/{iid}/criteria", json={"criteria": criteria})
        assert r.status_code == 200 and r.json()["acceptance_criteria"] == criteria
        assert publisher.post(f"{API}/issues/{iid}/fund").json()["state"] == "FUNDED"

        assert contributor.post(f"{API}/issues/{iid}/claim").json()["state"] == "CLAIMED"

        pr = PullRequest(
            repo=REPO, number=77, author="alice-dev", head_sha="c" * 40,
            body=f"Fixes #{5}", merged=False, files_changed=3, additions=40, deletions=2,
        )  # fmt: skip
        github.put_pull_request(pr)
        github.put_checks(REPO, "c" * 40, True)
        github.put_files(
            REPO,
            77,
            [ChangedFile("src/retry.py", 30, 2), ChangedFile("tests/test_retry.py", 10),
             ChangedFile("CHANGELOG.md", 1)],
        )  # fmt: skip
        submitted = contributor.post(f"{API}/issues/{iid}/submit", json={"pr_number": 77})
        assert submitted.status_code == 200 and submitted.json()["state"] == "IN_REVIEW"

        reviewed = publisher.post(f"{API}/issues/{iid}/review")
        assert reviewed.status_code == 200 and reviewed.json()["state"] == "ACCEPTED"

        merged = {
            "action": "closed",
            "repository": {"full_name": REPO},
            "pull_request": {
                "number": 77, "user": {"login": "alice-dev"}, "head": {"sha": "c" * 40},
                "body": "Fixes #5", "merged": True,
            },
        }  # fmt: skip
        hook = publisher.post(
            f"{API}/webhooks/github",
            content=json.dumps(merged),
            headers={"X-GitHub-Event": "pull_request", "Content-Type": "application/json"},
        )
        assert hook.status_code == 202
        rec = store.get(iid)
        assert rec is not None and rec.state is IssueState.PAID
        actions = [d.action for d in rec.decisions]
        for step in ("published", "criteria_approved", "price_approved", "claimed",
                     "submitted", "verdict_issued", "merged", "released"):  # fmt: skip
            assert step in actions, step
        assert rec.contributor_id == alice["party_id"]

    def test_only_the_claimants_own_pull_request_can_be_submitted(
        self, github: SimulatedGitHub
    ) -> None:
        contributor = TestClient(app)
        onboard(contributor, Wallet(41), "contributor", "bob", "bob-dev")
        contributor.post(f"{API}/issues/ISS-1001/claim")
        github.put_pull_request(
            PullRequest(
                repo="acme/ledger-core",
                number=9,
                author="mallory",
                head_sha="d" * 40,
                body="",
                merged=False,
                files_changed=1,
                additions=1,
                deletions=0,
            )  # fmt: skip
        )
        r = contributor.post(f"{API}/issues/ISS-1001/submit", json={"pr_number": 9})
        assert r.status_code == 403 and "mallory" in r.json()["detail"]


class TestWhoMayDoWhat:
    def test_an_unlinked_account_cannot_price_or_contribute(self) -> None:
        client = TestClient(app)
        sign_in(client, Wallet(51))
        client.post(f"{API}/auth/role", json={"role": "contributor", "name": "c"})
        r = client.post(f"{API}/issues/ISS-1001/claim")
        assert r.status_code == 403 and "link your GitHub" in r.json()["detail"]

    def test_a_publisher_acts_only_on_its_own_issues(self) -> None:
        client = TestClient(app)
        onboard(client, Wallet(52), "publisher", "Other Co", "other-co")
        r = client.post(f"{API}/issues/ISS-1006/criteria", json={"criteria": ["x"]})
        assert r.status_code == 403

    def test_a_contributor_cannot_publish(self) -> None:
        client = TestClient(app)
        onboard(client, Wallet(53), "contributor", "d", "d-dev")
        r = client.post(
            f"{API}/issues", json={"repo": "a/b", "title": "x", "publisher_id": "PUB-1"}
        )
        assert r.status_code == 403

    def test_outside_the_simulation_a_budget_is_its_publishers_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = TestClient(app)
        onboard(client, Wallet(54), "publisher", "Budget Co", "budget-co")
        mine = client.get(f"{API}/auth/me").json()["account"]["party_id"]
        monkeypatch.setattr(settings, "simulated", False)

        rows = {p["id"]: p for p in client.get(f"{API}/publishers").json()}
        assert rows[mine]["budget_remaining_usdc"] is not None
        others = [p for pid, p in rows.items() if pid != mine]
        assert others
        assert all(p["budget_remaining_usdc"] is None and p["approvers"] == [] for p in others)
        anonymous = TestClient(app).get(f"{API}/publishers").json()
        assert all(p["budget_remaining_usdc"] is None for p in anonymous)

    def test_outside_the_simulation_anonymous_writes_are_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "simulated", False)
        client = TestClient(app)
        assert (
            client.post(
                f"{API}/issues", json={"repo": "a/b", "title": "x", "publisher_id": "PUB-1"}
            ).status_code
            == 401
        )
        assert (
            client.post(f"{API}/issues/ISS-1006/criteria", json={"criteria": ["x"]}).status_code
            == 401
        )
        assert client.post(f"{API}/issues/ISS-1001/claim").status_code == 401
        # The demo stepper fabricates pull requests, so a deployment refuses it outright.
        assert client.post(f"{API}/issues/ISS-1006/advance").status_code == 403


class TestTheLinkCannotBeHijacked:
    """A link must be finished by the wallet that started it.

    Without that, an attacker starts a link with their own wallet, sends the authorize
    URL to a victim, and the victim's GitHub login is bound to the attacker's account.
    `github_login` is what gates publishing, claiming and submitting, so the attacker
    would then be paid for work the victim opened -- and the victim could never link
    their own login, because a login belongs to one wallet.
    """

    def _oauth(self, monkeypatch: pytest.MonkeyPatch, login: str) -> None:
        def github(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login/oauth/access_token":
                return httpx.Response(200, json={"access_token": "gho_once"})
            return httpx.Response(200, json={"login": login})

        client = GitHubOAuth("c", "s", transport=httpx.MockTransport(github))
        monkeypatch.setattr(auth_routes, "oauth", lambda: client)

    def test_another_wallets_session_cannot_finish_the_link(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._oauth(monkeypatch, "victim-dev")

        attacker = TestClient(app)
        sign_in(attacker, Wallet(31))
        attacker.post(f"{API}/auth/role", json={"role": "contributor", "name": "mallory"})
        start = attacker.post(f"{API}/auth/github/start").json()["authorize_url"]
        state = parse_qs(urlsplit(start).query)["state"][0]

        # The victim follows the link the attacker sent them, signed in as themselves.
        victim = TestClient(app)
        sign_in(victim, Wallet(32))
        victim.post(f"{API}/auth/role", json={"role": "contributor", "name": "ada"})
        r = victim.get(
            f"{API}/auth/github/callback", params={"code": "c0de", "state": state},
            follow_redirects=False,
        )  # fmt: skip
        assert r.status_code == 403

        # The victim's login was not bound to the attacker.
        assert victim.get(f"{API}/auth/me").json()["account"]["github_login"] is None
        assert attacker.get(f"{API}/auth/me").json()["account"]["github_login"] is None
        # And the state was spent, so it cannot be retried by the right wallet later.
        again = attacker.get(
            f"{API}/auth/github/callback", params={"code": "c0de", "state": state}
        )  # fmt: skip
        assert again.status_code == 400

    def test_an_anonymous_callback_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._oauth(monkeypatch, "someone")
        starter = TestClient(app)
        sign_in(starter, Wallet(33))
        starter.post(f"{API}/auth/role", json={"role": "contributor", "name": "a"})
        start = starter.post(f"{API}/auth/github/start").json()["authorize_url"]
        state = parse_qs(urlsplit(start).query)["state"][0]

        anonymous = TestClient(app)
        r = anonymous.get(f"{API}/auth/github/callback", params={"code": "c", "state": state})
        assert r.status_code == 401


class TestTheApiDoesNotCrashOnChosenInput:
    def test_a_timestamp_without_a_zone_is_a_refused_sign_in_not_a_500(self) -> None:
        client = TestClient(app)
        wallet = Wallet(34)
        nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
        # The message's own timestamp, with its zone stripped. RFC 3339 allows a
        # local time here, so this is input a caller can choose.
        import re

        naive = re.sub(
            r"Issued At: \S+", "Issued At: 2026-10-07T22:17:37", wallet.message(nonce)
        )
        assert "Issued At: 2026-10-07T22:17:37" in naive
        r = client.post(
            f"{API}/auth/verify", json={"message": naive, "signature": wallet.sign(naive)}
        )
        assert r.status_code == 401
class TestTheSimulatedGitHubFromTheBrowser:
    """What the web app needs to run the loop in the simulation, with nothing faked
    but GitHub: the claimant's pull request is opened, and the publisher merges it."""

    def _funded(self, publisher: TestClient) -> str:
        issue = publisher.post(
            f"{API}/issues", json={"repo": REPO, "title": "Retry on 503", "publisher_id": "x"}
        ).json()
        iid = issue["id"]
        assert issue["criteria_approved_at"] is None
        approved = publisher.post(
            f"{API}/issues/{iid}/criteria", json={"criteria": issue["acceptance_criteria"]}
        ).json()
        assert approved["criteria_approved_at"] is not None
        assert publisher.post(f"{API}/issues/{iid}/fund").json()["state"] == "FUNDED"
        return iid

    def test_the_loop_runs_from_publication_to_payment(self) -> None:
        publisher, contributor = TestClient(app), TestClient(app)
        onboard(publisher, Wallet(61), "publisher", "Acme", "acme-61")
        onboard(contributor, Wallet(62), "contributor", "eve", "eve-dev")
        iid = self._funded(publisher)
        contributor.post(f"{API}/issues/{iid}/claim")

        opened = contributor.post(f"{API}/demo/issues/{iid}/pull-request")
        assert opened.status_code == 200, opened.text
        assert opened.json()["author"] == "eve-dev"
        number = opened.json()["pr_number"]
        submitted = contributor.post(f"{API}/issues/{iid}/submit", json={"pr_number": number})
        assert submitted.json()["state"] == "IN_REVIEW"
        assert submitted.json()["submission"]["checks_passed"] is True
        assert contributor.post(f"{API}/issues/{iid}/review").json()["state"] == "ACCEPTED"

        merged = publisher.post(f"{API}/demo/issues/{iid}/merge")
        assert merged.status_code == 200, merged.text
        assert merged.json()["state"] == "PAID"
        actions = [d["action"] for d in publisher.get(f"{API}/issues/{iid}/timeline").json()]
        assert "merged" in actions and "released" in actions

    def test_only_the_claimant_opens_and_only_the_publisher_merges(self) -> None:
        publisher, contributor, other = TestClient(app), TestClient(app), TestClient(app)
        onboard(publisher, Wallet(63), "publisher", "Acme", "acme-63")
        onboard(contributor, Wallet(64), "contributor", "fay", "fay-dev")
        onboard(other, Wallet(65), "contributor", "gus", "gus-dev")
        iid = self._funded(publisher)
        assert other.post(f"{API}/demo/issues/{iid}/pull-request").status_code == 403
        contributor.post(f"{API}/issues/{iid}/claim")
        assert other.post(f"{API}/demo/issues/{iid}/pull-request").status_code == 403
        number = contributor.post(f"{API}/demo/issues/{iid}/pull-request").json()["pr_number"]
        contributor.post(f"{API}/issues/{iid}/submit", json={"pr_number": number})
        assert contributor.post(f"{API}/demo/issues/{iid}/merge").status_code == 403

    def test_a_release_over_the_threshold_says_it_awaits_an_approver(self) -> None:
        publisher, contributor, cfo = TestClient(app), TestClient(app), TestClient(app)
        held = held_release(publisher, contributor, seed=66, approvers=["cfo-acme"])
        assert held["state"] == "ACCEPTED" and held["awaiting_approver"] is True
        approver(cfo, Wallet(68), "cfo-acme")
        released = cfo.post(f"{API}/issues/{held['id']}/approve-release").json()
        assert released["state"] == "PAID" and released["awaiting_approver"] is False

    def test_outside_the_simulation_github_is_real(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "simulated", False)
        client = TestClient(app)
        assert client.post(f"{API}/demo/issues/ISS-1003/pull-request").status_code == 403
        assert client.post(f"{API}/demo/issues/ISS-1004/merge").status_code == 403


def held_release(
    publisher: TestClient, contributor: TestClient, *, seed: int, approvers: list[str]
) -> dict:
    """Onboard a Team publisher whose policy holds every release for `approvers`, and
    a contributor, and run one issue of theirs to the merge, where the payout waits."""
    account = onboard(publisher, Wallet(seed), "publisher", "Acme", f"acme-{seed}")
    onboard(contributor, Wallet(seed + 1), "contributor", "hal", f"hal-{seed}")
    # Approval thresholds are in the Team plan, bought here without a conversation.
    pid = account["party_id"]
    assert publisher.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"})
    paid = publisher.post(f"{API}/demo/publishers/{pid}/subscription/pay").json()
    assert paid["plan"] == "team"
    policy = publisher.put(
        f"{API}/publishers/{pid}/policy",
        json={"approval_threshold_usdc": "1", "approvers": approvers},
    )
    assert policy.status_code == 200, policy.text
    iid = TestTheSimulatedGitHubFromTheBrowser()._funded(publisher)
    contributor.post(f"{API}/issues/{iid}/claim")
    number = contributor.post(f"{API}/demo/issues/{iid}/pull-request").json()["pr_number"]
    contributor.post(f"{API}/issues/{iid}/submit", json={"pr_number": number})
    contributor.post(f"{API}/issues/{iid}/review")
    held = publisher.post(f"{API}/demo/issues/{iid}/merge").json()
    assert held["awaiting_approver"] is True, held
    return held


def approver(client: TestClient, wallet: Wallet, login: str) -> dict:
    """A named approver signs in with their own wallet and links their GitHub login.
    An account has one role and linking needs one, so they take the contributor's."""
    return onboard(client, wallet, "contributor", login, login)


class TestTheApproverIsWhoTheSessionProves:
    """A release over the threshold is approved by the signed-in account whose linked
    GitHub login the organisation named, never by a name the request supplies."""

    def test_a_named_approver_approves_signed_in_as_themselves(self) -> None:
        publisher, contributor, dana = TestClient(app), TestClient(app), TestClient(app)
        held = held_release(publisher, contributor, seed=71, approvers=["@Dana-Acme"])
        approver(dana, Wallet(73), "dana-acme")

        r = dana.post(f"{API}/issues/{held['id']}/approve-release")

        assert r.status_code == 200, r.text
        assert r.json()["state"] == "PAID"
        rec = store.get(held["id"])
        assert rec is not None
        [approved] = [d for d in rec.decisions if d.action == "release_approved"]
        assert approved.outcome.startswith("dana-acme approved the release of")

    def test_naming_an_approver_in_the_request_approves_nothing(self) -> None:
        """The finding on #92: the publisher's own session sends a named approver's
        name, and the payout must stay where it is."""
        publisher, contributor = TestClient(app), TestClient(app)
        held = held_release(publisher, contributor, seed=74, approvers=["dana-acme"])

        r = publisher.post(
            f"{API}/issues/{held['id']}/approve-release", json={"approver": "dana-acme"}
        )

        assert r.status_code == 403
        assert "acme-74 is not one of" in r.json()["detail"]
        rec = store.get(held["id"])
        assert rec is not None and rec.state is IssueState.ACCEPTED and rec.paid is None

    def test_the_organisations_own_account_approves_only_if_its_login_is_named(self) -> None:
        publisher, contributor = TestClient(app), TestClient(app)
        held = held_release(publisher, contributor, seed=76, approvers=["acme-76"])
        r = publisher.post(f"{API}/issues/{held['id']}/approve-release")
        assert r.status_code == 200 and r.json()["state"] == "PAID"

    def test_an_account_nobody_named_cannot_approve(self) -> None:
        publisher, contributor, mallory = TestClient(app), TestClient(app), TestClient(app)
        held = held_release(publisher, contributor, seed=78, approvers=["dana-acme"])
        approver(mallory, Wallet(80), "mallory")
        assert mallory.post(f"{API}/issues/{held['id']}/approve-release").status_code == 403

    def test_the_contributor_being_paid_cannot_approve_their_own_payout(self) -> None:
        publisher, contributor = TestClient(app), TestClient(app)
        held = held_release(publisher, contributor, seed=81, approvers=["dana-acme", "hal-81"])
        r = contributor.post(f"{API}/issues/{held['id']}/approve-release")
        assert r.status_code == 403
        assert "their own payout" in r.json()["detail"]

    def test_an_account_without_a_linked_login_is_told_to_link_one(self) -> None:
        publisher, contributor, unlinked = TestClient(app), TestClient(app), TestClient(app)
        held = held_release(publisher, contributor, seed=83, approvers=["dana-acme"])
        sign_in(unlinked, Wallet(85))
        assert unlinked.post(f"{API}/auth/role", json={"role": "contributor", "name": "x"})
        r = unlinked.post(f"{API}/issues/{held['id']}/approve-release")
        assert r.status_code == 403 and "link your GitHub account" in r.json()["detail"]

    def test_outside_the_simulation_an_anonymous_request_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "simulated", False)
        r = TestClient(app).post(
            f"{API}/issues/ISS-1006/approve-release", json={"approver": "dana-acme"}
        )
        assert r.status_code == 401


class TestCriteriaAReviewerCanJudge:
    """#38: the publisher cannot approve criteria a reviewer could not judge."""

    def test_vague_criteria_are_refused_with_a_reason_for_each(self) -> None:
        client = TestClient(app)
        r = client.post(
            f"{API}/issues/ISS-1006/criteria",
            json={"criteria": ["A test reproduces the round-down on 0.005.", "It works properly.",
                               "Is it fast?"]},
        )  # fmt: skip
        assert r.status_code == 422
        reasons = [d["msg"] for d in r.json()["detail"]]
        assert reasons[0].startswith("Criterion 2 says \"works\"")
        assert reasons[1].startswith("Criterion 3 is a question")
        assert store.get("ISS-1006").criteria_approved_at is None  # type: ignore[union-attr]

    def test_drafted_criteria_can_be_approved_as_they_stand(self) -> None:
        client = TestClient(app)
        issue = client.post(
            f"{API}/issues",
            json={"repo": "acme/x", "title": "CSV export breaks on commas inside fields",
                  "labels": ["bug"], "publisher_id": "PUB-1"},
        ).json()  # fmt: skip
        approved = client.post(
            f"{API}/issues/{issue['id']}/criteria",
            json={"criteria": issue["acceptance_criteria"]},
        )
        assert approved.status_code == 200
        assert any("CSV export breaks" in c for c in issue["acceptance_criteria"])

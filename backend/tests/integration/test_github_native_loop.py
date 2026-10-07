"""Epic #6: an issue goes from a label in a real repository to a payout on GitHub events.

After each person signs in once (a wallet, a side, a linked GitHub login), nothing
here touches the web app. The App's installation connects the repository, a label
prices the issue, comments fund and claim it, the pull request is submitted by its
own event, the sweeper reviews it, and the merge releases the payment. GitHub is
the simulated gateway; every delivery goes through the signed webhook receiver.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from siwe_wallet import Wallet

from misthos.api.v1.webhooks import signature_for
from misthos.config import settings
from misthos.domain.issue import IssueState
from misthos.domain.review import ChangedFile
from misthos.domain.signals import IssueFacts
from misthos.main import app
from misthos.services.github import PullRequest, SimulatedGitHub
from misthos.store import store
from misthos.workers.sweeper import sweep_once

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


def onboard(seed: int, side: str, login: str) -> str:
    client = TestClient(app)
    wallet = Wallet(seed)
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    client.post(f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)})
    party = client.post(f"{API}/auth/role", json={"role": side, "name": login}).json()["party_id"]
    client.post(f"{API}/auth/github/simulate", json={"login": login})
    return party


def deliver(event: str, payload: dict[str, Any]) -> dict[str, Any]:
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
    return r.json()


def installed(by: str, repos: list[str] = [REPO]) -> dict[str, Any]:  # noqa: B006
    return deliver("installation", {
        "action": "created", "installation": {"id": 42}, "sender": {"login": by},
        "repositories": [{"full_name": r} for r in repos],
    })  # fmt: skip


def labelled(number: int, *, label: str = "misthos") -> dict[str, Any]:
    return deliver("issues", {
        "action": "labeled", "repository": {"full_name": REPO}, "label": {"name": label},
        "issue": {"number": number, "title": "Retry on 503",
                  "labels": [{"name": "bug"}, {"name": label}]},
    })  # fmt: skip


def commented(number: int, login: str, body: str, *, bot: bool = False, on_pr: bool = False):
    issue: dict[str, Any] = {"number": number}
    if on_pr:
        issue["pull_request"] = {"url": "https://api.github.com/x"}
    return deliver("issue_comment", {
        "action": "created", "repository": {"full_name": REPO}, "issue": issue,
        "comment": {"body": body, "user": {"login": login, "type": "Bot" if bot else "User"}},
        "sender": {"login": login, "type": "Bot" if bot else "User"},
    })  # fmt: skip


def comments(github: SimulatedGitHub, number: int) -> list[str]:
    return [s.body for s in github.sent if s.kind == "comment" and s.target == str(number)]


class TestFromALabelToAPayout:
    def test_the_whole_loop_runs_on_github_events(self, github: SimulatedGitHub) -> None:
        publisher = onboard(81, "publisher", "acme-maint")
        contributor = onboard(82, "contributor", "dev-ana")
        github.put_issue(IssueFacts(repo=REPO, number=12, title="Retry on 503",
                                    body="`src/retry.py` gives up on the first 503.",
                                    labels=("bug", "misthos")))  # fmt: skip

        assert "for " + publisher in installed("acme-maint")["outcome"]
        priced = labelled(12)
        assert priced["handled"] is True
        rec = store.find_open(REPO, 12)
        assert rec is not None and rec.state is IssueState.AWAITING_APPROVAL
        assert rec.publisher_id == publisher
        assert "Priced on Misthos" in comments(github, 12)[-1]

        # Only the publisher's own login can commit its money.
        commented(12, "dev-ana", "/misthos approve")
        assert "only the publisher" in comments(github, 12)[-1]
        commented(12, "acme-maint", "Looks right.\n/misthos approve")
        assert store.get(rec.id).state is IssueState.FUNDED  # type: ignore[union-attr]
        assert "Funded on Misthos" in comments(github, 12)[-1]

        commented(12, "dev-ana", "/misthos claim")
        claimed = store.get(rec.id)
        assert claimed is not None and claimed.state is IssueState.CLAIMED
        assert claimed.contributor_id == contributor
        assert "Claimed by @dev-ana" in comments(github, 12)[-1]

        github.put_pull_request(PullRequest(
            repo=REPO, number=13, author="dev-ana", head_sha="e" * 40, body="Fixes #12",
            merged=False, files_changed=3, additions=40, deletions=2,
        ))  # fmt: skip
        github.put_files(REPO, 13, [ChangedFile("src/retry.py", 30, 2),
                                    ChangedFile("tests/test_retry.py", 10),
                                    ChangedFile("CHANGELOG.md", 1)])  # fmt: skip
        github.put_checks(REPO, "e" * 40, True)
        pr = {"number": 13, "user": {"login": "dev-ana"}, "head": {"sha": "e" * 40},
              "body": "Fixes #12", "merged": False, "draft": False}  # fmt: skip
        deliver("pull_request", {"action": "opened", "repository": {"full_name": REPO},
                                 "pull_request": pr})  # fmt: skip
        assert store.get(rec.id).state is IssueState.IN_REVIEW  # type: ignore[union-attr]

        # Nobody asks for the review: the sweeper runs it and posts it on the pull request.
        report = sweep_once(store)
        assert report.reviewed.get(rec.id) == "accept"
        assert any(s.kind == "review" and s.target == "13" for s in github.sent)

        deliver("pull_request", {"action": "closed", "repository": {"full_name": REPO},
                                 "pull_request": {**pr, "merged": True}})  # fmt: skip
        paid = store.get(rec.id)
        assert paid is not None and paid.state is IssueState.PAID
        steps = [d.action for d in paid.decisions]
        for step in ("published", "criteria_approved", "price_approved", "claimed",
                     "submitted", "verdict_issued", "merged", "released"):  # fmt: skip
            assert step in steps, step


class TestConnections:
    def test_an_installer_who_signs_in_later_is_picked_up(self, github: SimulatedGitHub) -> None:
        installed("late-maint")
        labelled(5)
        assert store.find_open(REPO, 5) is None
        assert "has not signed in as a publisher" in comments(github, 5)[-1]
        onboard(83, "publisher", "late-maint")
        labelled(5)
        assert store.find_open(REPO, 5) is not None

    def test_an_unconnected_repository_is_left_alone(self, github: SimulatedGitHub) -> None:
        assert labelled(7)["handled"] is False
        assert comments(github, 7) == []

    def test_other_labels_do_not_list_an_issue(self) -> None:
        onboard(84, "publisher", "acme-maint")
        installed("acme-maint")
        labelled(8, label="enhancement")
        assert store.find_open(REPO, 8) is None

    def test_uninstalling_disconnects(self) -> None:
        onboard(85, "publisher", "acme-maint")
        installed("acme-maint")
        deliver("installation", {"action": "deleted", "installation": {"id": 42},
                                 "sender": {"login": "acme-maint"}})  # fmt: skip
        assert store.connection(REPO) is None

    def test_repositories_added_and_removed_follow_the_installation(self) -> None:
        publisher = onboard(86, "publisher", "acme-maint")
        installed("acme-maint")
        deliver("installation_repositories", {
            "action": "added", "installation": {"id": 42}, "sender": {"login": "acme-maint"},
            "repositories_added": [{"full_name": "acme/gears"}],
            "repositories_removed": [{"full_name": REPO}],
        })  # fmt: skip
        assert [c.repo for c in store.connections_of(publisher)] == ["acme/gears"]


class TestCommands:
    @pytest.fixture
    def listed(self, github: SimulatedGitHub) -> str:
        onboard(87, "publisher", "acme-maint")
        installed("acme-maint")
        labelled(12)
        rec = store.find_open(REPO, 12)
        assert rec is not None
        return rec.id

    def test_the_publisher_can_replace_the_criteria(
        self, listed: str, github: SimulatedGitHub
    ) -> None:
        commented(12, "acme-maint", "/misthos criteria\n- A test reproduces the 503 from "
                  "`src/retry.py`.\n- The changelog records the change.")  # fmt: skip
        rec = store.get(listed)
        assert rec is not None and rec.criteria_approved_at is not None
        assert rec.acceptance_criteria[0].startswith("A test reproduces the 503")
        commented(12, "acme-maint", "/misthos approve")
        assert store.get(listed).acceptance_criteria == rec.acceptance_criteria  # type: ignore[union-attr]

    def test_uncheckable_criteria_are_refused_with_reasons(
        self, listed: str, github: SimulatedGitHub
    ) -> None:
        commented(12, "acme-maint", "/misthos criteria\n- It works properly.")
        assert "cannot be judged" in comments(github, 12)[-1]
        assert store.get(listed).criteria_approved_at is None  # type: ignore[union-attr]

    def test_strangers_and_unlinked_logins_are_told_what_to_do(
        self, listed: str, github: SimulatedGitHub
    ) -> None:
        commented(12, "nobody-here", "/misthos claim")
        assert "sign in" in comments(github, 12)[-1]
        commented(12, "acme-maint", "/misthos frobnicate")
        assert "is not a command" in comments(github, 12)[-1]

    def test_bots_pull_requests_and_chatter_are_ignored(
        self, listed: str, github: SimulatedGitHub
    ) -> None:
        before = len(github.sent)
        assert commented(12, "misthos[bot]", "/misthos approve", bot=True)["handled"] is False
        assert commented(12, "acme-maint", "/misthos approve", on_pr=True)["handled"] is False
        assert commented(12, "acme-maint", "Thanks, looks good")["handled"] is False
        assert len(github.sent) == before
        assert store.get(listed).state is IssueState.AWAITING_APPROVAL  # type: ignore[union-attr]

    def test_help_lists_the_commands(self, github: SimulatedGitHub) -> None:
        commented(99, "anyone", "/misthos help")
        assert "/misthos claim" in comments(github, 99)[-1]


class TestThePublishersView:
    def test_a_publisher_sees_its_connected_repositories(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "github_app_slug", "misthos-dev")
        publisher = onboard(88, "publisher", "acme-maint")
        installed("acme-maint", [REPO, "acme/gears"])
        body = TestClient(app).get(f"{API}/publishers/{publisher}/repositories").json()
        assert [r["repo"] for r in body["repositories"]] == ["acme/gears", REPO]
        assert body["label"] == "misthos"
        assert body["install_url"] == "https://github.com/apps/misthos-dev/installations/new"

"""Epic #6: an issue goes from a label in a real repository to a payout on GitHub events.

After each person signs in once (a wallet, a side, a linked GitHub login), nothing
here touches the web app. The App's installation connects the repository, a label
prices the issue, comments fund and claim it, the pull request is submitted by its
own event, the sweeper reviews it, and the merge releases the payment. GitHub is
the simulated gateway; every delivery goes through the signed webhook receiver.
"""

from __future__ import annotations

import json
import logging
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
from misthos.services.coordination import Busy
from misthos.services.github import Installation, PullRequest
from misthos.store import store
from misthos.workers.sweeper import sweep_once
from tests.support.fake_github import FakeGitHub

API = "/api/v1"
REPO = "acme/widgets"




@pytest.fixture
def github() -> FakeGitHub:
    assert isinstance(store.github, FakeGitHub)
    return store.github


def onboard(seed: int, side: str, login: str) -> str:
    client = TestClient(app)
    wallet = Wallet(seed)
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    client.post(f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)})
    created = client.post(f"{API}/auth/role", json={"role": side, "name": login}).json()
    store.link_github(created["address"], login)
    return created["party_id"]


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


def comments(github: FakeGitHub, number: int) -> list[str]:
    return [s.body for s in github.sent if s.kind == "comment" and s.target == str(number)]


class TestFromALabelToAPayout:
    def test_the_whole_loop_runs_on_github_events(self, github: FakeGitHub) -> None:
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
        # The label lists it with no price on it: the engine's suggestion is on the
        # page and the publisher names the number (#142).
        assert rec.price is None
        assert "Listed on Misthos, with no price yet" in comments(github, 12)[-1]

        # Only the publisher's own login can name the price and commit its money.
        commented(12, "dev-ana", "/misthos price 250")
        assert "only the publisher" in comments(github, 12)[-1]

        # And nothing is funded until the publisher names what the work is worth.
        commented(12, "acme-maint", "Looks right.\n/misthos approve")
        assert store.get(rec.id).state is IssueState.AWAITING_APPROVAL  # type: ignore[union-attr]
        assert "no price" in comments(github, 12)[-1]

        commented(12, "acme-maint", "/misthos price 250")
        assert "priced this at 250.00 USDC" in comments(github, 12)[-1]
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
        for step in ("price_set", "criteria_approved", "price_approved", "claimed",
                     "submitted", "verdict_issued", "merged", "released"):  # fmt: skip
            assert step in steps, step


class TestConnections:
    def test_an_installer_who_signs_in_later_is_picked_up(self, github: FakeGitHub) -> None:
        installed("late-maint")
        labelled(5)
        assert store.find_open(REPO, 5) is None
        assert "has not signed in as a publisher" in comments(github, 5)[-1]
        onboard(83, "publisher", "late-maint")
        labelled(5)
        assert store.find_open(REPO, 5) is not None

    def test_an_unconnected_repository_is_told_what_the_label_needs(
        self, github: FakeGitHub, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A refused label answers on the issue and says so in the log.

        Nothing is listed, and that has to be visible where the person is looking. A
        refusal that posts nothing and logs nothing reads as the platform ignoring the
        label, which is what a failed delivery looks like from the outside (#180).
        """
        with caplog.at_level(logging.WARNING, logger="misthos.github.events"):
            assert labelled(7)["handled"] is False

        assert "no publisher is connected to" in comments(github, 7)[-1]
        assert any("no publisher" in r.getMessage() for r in caplog.records)

    def test_a_repository_the_app_is_on_is_connected_from_its_installation(
        self, github: FakeGitHub
    ) -> None:
        """GitHub is asked which installation covers the repository, and it is recorded.

        `repo_connections` is written by the `installation_repositories` webhook and by
        nothing else, and GitHub does not redeliver a delivery that failed, so a
        repository the App is on can have no row and no way to get one. That used to
        refuse the label for ever; GitHub still knows, so the label asks it (#180).
        """
        publisher = onboard(91, "publisher", "acme-maint")
        github.installations = [Installation(id=42, login="acme-maint", repositories=(REPO,))]
        assert store.connection(REPO) is None

        assert labelled(9)["handled"] is True
        rec = store.find_open(REPO, 9)
        assert rec is not None and rec.publisher_id == publisher
        connection = store.connection(REPO)
        assert connection is not None and connection.publisher_id == publisher

    def test_the_sweeper_records_a_connection_no_delivery_wrote(
        self, github: FakeGitHub
    ) -> None:
        """The missing row heals on its own, without anyone labelling an issue.

        A label is not the only thing a connection is for: the web app lists a
        publisher's repositories from it. Reconcile is the pass that makes the database
        agree with GitHub's installations instead of with whichever deliveries happened
        to arrive (#180).
        """
        publisher = onboard(92, "publisher", "acme-maint")
        github.installations = [Installation(id=42, login="acme-maint", repositories=(REPO,))]

        report = sweep_once(store)

        assert report.connections_added == (REPO,)
        connection = store.connection(REPO)
        assert connection is not None and connection.publisher_id == publisher

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
    def listed(self, github: FakeGitHub) -> str:
        onboard(87, "publisher", "acme-maint")
        installed("acme-maint")
        labelled(12)
        rec = store.find_open(REPO, 12)
        assert rec is not None
        return rec.id

    def test_the_publisher_can_replace_the_criteria(
        self, listed: str, github: FakeGitHub
    ) -> None:
        commented(12, "acme-maint", "/misthos criteria\n- A test reproduces the 503 from "
                  "`src/retry.py`.\n- The changelog records the change.")  # fmt: skip
        rec = store.get(listed)
        assert rec is not None and rec.criteria_approved_at is not None
        assert rec.acceptance_criteria[0].startswith("A test reproduces the 503")
        commented(12, "acme-maint", "/misthos price 250")
        commented(12, "acme-maint", "/misthos approve")
        assert store.get(listed).acceptance_criteria == rec.acceptance_criteria  # type: ignore[union-attr]

    def test_uncheckable_criteria_are_refused_with_reasons(
        self, listed: str, github: FakeGitHub
    ) -> None:
        commented(12, "acme-maint", "/misthos criteria\n- It works properly.")
        assert "cannot be judged" in comments(github, 12)[-1]
        assert store.get(listed).criteria_approved_at is None  # type: ignore[union-attr]

    def test_strangers_and_unlinked_logins_are_told_what_to_do(
        self, listed: str, github: FakeGitHub
    ) -> None:
        commented(12, "nobody-here", "/misthos claim")
        assert "sign in" in comments(github, 12)[-1]
        commented(12, "acme-maint", "/misthos frobnicate")
        assert "is not a command" in comments(github, 12)[-1]

    def test_bots_pull_requests_and_chatter_are_ignored(
        self, listed: str, github: FakeGitHub
    ) -> None:
        before = len(github.sent)
        assert commented(12, "misthos[bot]", "/misthos approve", bot=True)["handled"] is False
        assert commented(12, "acme-maint", "/misthos approve", on_pr=True)["handled"] is False
        assert commented(12, "acme-maint", "Thanks, looks good")["handled"] is False
        assert len(github.sent) == before
        assert store.get(listed).state is IssueState.AWAITING_APPROVAL  # type: ignore[union-attr]

    def test_help_lists_the_commands(self, github: FakeGitHub) -> None:
        commented(99, "anyone", "/misthos help")
        assert "/misthos claim" in comments(github, 99)[-1]


class TestCommandsInCodeAndQuotes:
    """A command shown as an example, or quoted from someone else, is not one: for a
    publisher, an accidental `/misthos approve` commits escrow funds."""

    @pytest.fixture
    def listed(self, github: FakeGitHub) -> str:
        onboard(89, "publisher", "acme-maint")
        installed("acme-maint")
        labelled(12)
        rec = store.find_open(REPO, 12)
        assert rec is not None
        return rec.id

    @pytest.mark.parametrize(
        "body",
        [
            "Should I run this?\n\n```\n/misthos approve\n```\n",
            "~~~\n/misthos approve\n~~~",
            "```text\n/misthos approve\n```",
            "````markdown\n```\n/misthos approve\n```\n````",
            "Example:\n\n    /misthos approve\n",
            "\t/misthos approve",
            "> /misthos approve",
            "> Should I?\n>\n> /misthos approve\n\nNot yet.",
            "<!--\n/misthos approve\n-->",
            "```\nnever closed\n/misthos approve",
        ],
        ids=[
            "fenced", "tilde-fence", "info-string", "longer-fence", "indented", "tab",
            "quote", "quote-reply", "html-comment", "unclosed-fence",
        ],
    )  # fmt: skip
    def test_a_command_in_code_a_quote_or_a_comment_does_not_run(
        self, listed: str, github: FakeGitHub, body: str
    ) -> None:
        before = len(comments(github, 12))
        out = commented(12, "acme-maint", body)
        assert store.get(listed).state is IssueState.AWAITING_APPROVAL  # type: ignore[union-attr]
        assert out["handled"] is False
        assert len(comments(github, 12)) == before

    def test_a_command_after_a_closed_fence_or_indented_three_spaces_runs(
        self, listed: str
    ) -> None:
        commented(12, "acme-maint", "Like this:\n\n```\n/misthos claim\n```\n   /misthos price 250")
        rec = store.get(listed)
        assert rec is not None and rec.price is not None
        assert rec.price.decimal == 250


class TestSeveralCommandsInOneComment:
    @pytest.fixture
    def listed(self, github: FakeGitHub) -> str:
        onboard(90, "publisher", "acme-maint")
        installed("acme-maint")
        labelled(12)
        rec = store.find_open(REPO, 12)
        assert rec is not None
        return rec.id

    CRITERIA = (
        "/misthos criteria\n"
        "- A test reproduces the 503 from `src/retry.py`.\n"
        "- The changelog records the change.\n"
    )

    def test_every_command_line_runs_in_order(self, listed: str, github: FakeGitHub) -> None:
        commented(12, "acme-maint", self.CRITERIA + "/misthos price 250\n/misthos approve")
        rec = store.get(listed)
        assert rec is not None and rec.state is IssueState.FUNDED
        # The approve saw the criteria just approved, and funded those, not the draft.
        assert rec.acceptance_criteria == [
            "A test reproduces the 503 from `src/retry.py`.",
            "The changelog records the change.",
        ]
        assert "Funded on Misthos" in comments(github, 12)[-2]
        assert "Criteria approved by @acme-maint" in comments(github, 12)[-1]

    def test_a_busy_issue_stops_the_comment_without_running_it_twice(
        self, listed: str, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failing the delivery for GitHub to retry would approve the criteria again."""

        def busy(issue_id: str, by: str) -> None:
            raise Busy(issue_id)

        monkeypatch.setattr(store.current(), "approve_price", busy)
        out = commented(12, "acme-maint", self.CRITERIA + "/misthos approve")
        rec = store.get(listed)
        assert rec is not None and rec.state is IssueState.AWAITING_APPROVAL
        assert [d.action for d in rec.decisions].count("criteria_approved") == 1
        assert "was busy, so `/misthos approve` was not run" in comments(github, 12)[-1]
        assert "/misthos approve did not run" in out["outcome"]

    def test_a_refused_criteria_never_falls_through_to_approve(
        self, listed: str, github: FakeGitHub
    ) -> None:
        drafted = store.get(listed).acceptance_criteria  # type: ignore[union-attr]
        commented(12, "acme-maint", "/misthos criteria\n- It works properly.\n/misthos approve")
        rec = store.get(listed)
        assert rec is not None and rec.state is IssueState.AWAITING_APPROVAL
        assert rec.criteria_approved_at is None and rec.acceptance_criteria == drafted
        reply = comments(github, 12)[-1]
        assert "cannot be judged" in reply
        assert "The rest of this comment was not run" in reply and "/misthos approve" in reply

    def test_a_comment_gets_one_reply_for_all_its_commands(
        self, listed: str, github: FakeGitHub
    ) -> None:
        before = len(comments(github, 12))
        out = commented(12, "acme-maint", "/misthos help\n" + self.CRITERIA)
        assert len(comments(github, 12)) == before + 1
        reply = comments(github, 12)[-1]
        assert "**Misthos commands**" in reply and "Criteria approved by @acme-maint" in reply
        assert "/misthos help ran" in out["outcome"] and "/misthos criteria ran" in out["outcome"]

    def test_only_the_first_five_commands_in_a_comment_run(
        self, listed: str, github: FakeGitHub
    ) -> None:
        commented(12, "acme-maint", "/misthos help\n" * 6)
        reply = comments(github, 12)[-1]
        assert reply.count("**Misthos commands**") == 5
        assert "Only the first 5 commands in a comment are run" in reply


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
        assert body["app_name"] == "misthos-dev"
        # The installation that put those repositories here is the one to manage.
        assert body["settings_url"] == "https://github.com/settings/installations/42"

    def test_a_connected_repository_offers_its_installation_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once installed, the button manages the installation rather than installing.

        GitHub's per-installation page is the only place a repository is added to or
        removed from an installation, so it is what to point at instead of an install
        page the App is already on.
        """
        monkeypatch.setattr(settings, "github_app_slug", "misthos-dev")
        publisher = onboard(90, "publisher", "acme-maint")
        installed("acme-maint", [REPO])
        body = TestClient(app).get(f"{API}/publishers/{publisher}/repositories").json()

        assert [r["repo"] for r in body["repositories"]] == [REPO]
        assert body["settings_url"] == "https://github.com/settings/installations/42"
        assert body["app_name"] == "misthos-dev"

    def test_an_installation_waits_for_the_wallet_that_claims_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Install first and link later, and the repository appears.

        This is the path taken when GitHub does not send the browser back from the
        install -- the setting is not documented to, and it did not in practice. The
        installation is then recorded with the login that made it and no publisher,
        because no wallet is linked to that login yet. Linking is what claims it, and
        the next read resolves it: `connections_of` walks every connection through
        `connection()`, which re-resolves an unclaimed row by its `installed_by`.
        Without that, an installer would be told to install again for ever.
        """
        monkeypatch.setattr(settings, "github_app_slug", "misthos-dev")
        installed("late-maint")  # nobody is linked to that login yet: unclaimed

        client = TestClient(app)
        wallet = Wallet(89)
        nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
        message = wallet.message(nonce)
        client.post(
            f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)}
        )
        created = client.post(
            f"{API}/auth/role", json={"role": "publisher", "name": "late-maint"}
        ).json()
        publisher = created["party_id"]

        # Linked to nothing: the installation is invisible to this publisher, and so
        # there is no installation to manage yet.
        before = client.get(f"{API}/publishers/{publisher}/repositories").json()
        assert before["repositories"] == []
        assert before["settings_url"] is None

        store.link_github(created["address"], "late-maint")

        after = client.get(f"{API}/publishers/{publisher}/repositories").json()
        assert [r["repo"] for r in after["repositories"]] == [REPO]


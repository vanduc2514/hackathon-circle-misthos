"""GitHub, end to end through the webhook receiver: pull request events move the
lifecycle, a merge releases the money, and what the platform posts back lands on
the issue and the pull request. GitHub is the simulated one, so every post can be
read back; tests/unit/test_github_app.py proves the real client sends the same."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from misthos.api.v1.webhooks import signature_for
from misthos.config import settings
from misthos.domain.issue import IssueState
from misthos.domain.ledger import MoneyEventKind
from misthos.domain.signals import IssueFacts, TreeCounts
from misthos.main import app
from misthos.models.records import IssueRecord
from misthos.services.github import SimulatedGitHub
from misthos.services.github.events import closed_issues
from misthos.store import store

API = "/api/v1"
HOOK = f"{API}/webhooks/github"


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def github() -> SimulatedGitHub:
    assert isinstance(store.github, SimulatedGitHub)
    return store.github


def get(issue_id: str) -> IssueRecord:
    rec = store.get(issue_id)
    assert rec is not None
    return rec


def deliver(
    client: TestClient, event: str, payload: dict[str, Any], *, delivery: str | None = None
) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    r = client.post(
        HOOK,
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
            "X-Hub-Signature-256": signature_for(body, settings.github_webhook_secret),
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 202, r.text
    return r.json()


def pull_request(
    repo: str,
    number: int,
    author: str,
    *,
    action: str = "opened",
    body: str = "",
    sha: str = "a" * 40,
    merged: bool = False,
    draft: bool = False,
) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {
            "number": number,
            "user": {"login": author},
            "head": {"sha": sha},
            "body": body,
            "merged": merged,
            "draft": draft,
            "changed_files": 4,
            "additions": 51,
            "deletions": 7,
        },
    }


CLAIMED = ("ISS-1003", "globex/parse-locale", 87, "amara.dev")  # seeded CLAIMED
IN_REVIEW = ("ISS-1002", "acme/ledger-core", 903)  # seeded IN_REVIEW, PR #903
REWORK = ("ISS-1007", "northwind/httpx-retry", 640, "tom_e")  # seeded REWORK, PR #640


class TestLinking:
    @pytest.mark.parametrize(
        ("body", "found"),
        [
            ("Fixes #87", [87]),
            ("closes #87 and resolves #12", [87, 12]),
            ("Resolved: globex/parse-locale#87", [87]),
            ("fixes other/repo#87", []),
            ("See #87", []),
            ("fixes #87, fixes #87", [87]),
        ],
    )
    def test_a_pull_request_closes_what_its_body_says(self, body: str, found: list[int]) -> None:
        assert closed_issues(body, "globex/parse-locale") == found


class TestSubmission:
    def test_the_claimants_pull_request_moves_the_issue_to_review(
        self, client: TestClient, github: SimulatedGitHub
    ) -> None:
        issue_id, repo, number, author = CLAIMED
        github.history[(repo, author)] = 3
        out = deliver(
            client, "pull_request", pull_request(repo, 501, author, body=f"Fixes #{number}")
        )

        assert out["handled"] and out["issue_ids"] == [issue_id]
        rec = get(issue_id)
        assert rec.state is IssueState.IN_REVIEW
        assert rec.submission is not None
        assert (rec.submission.pr_number, rec.submission.head_sha) == (501, "a" * 40)
        assert rec.submission.additions == 51
        assert rec.decisions[-1].action == "submitted"
        assert "3 earlier pull request(s)" in rec.decisions[-1].outcome
        # The pull request shows that the review has started.
        assert [(s.kind, s.state) for s in github.sent] == [("status", "pending")]

    def test_someone_elses_pull_request_does_not_take_the_claim(
        self, client: TestClient, github: SimulatedGitHub
    ) -> None:
        issue_id, repo, number, _ = CLAIMED
        out = deliver(
            client, "pull_request", pull_request(repo, 502, "drive-by", body=f"Fixes #{number}")
        )
        assert out["handled"] is False
        assert "holds the claim" in out["outcome"]
        assert get(issue_id).state is IssueState.CLAIMED
        assert github.sent == []

    def test_a_draft_waits_until_it_is_ready(self, client: TestClient) -> None:
        issue_id, repo, number, author = CLAIMED
        draft = pull_request(repo, 501, author, body=f"Fixes #{number}", draft=True)
        assert deliver(client, "pull_request", draft)["handled"] is False
        ready = pull_request(repo, 501, author, body=f"Fixes #{number}", action="ready_for_review")
        assert deliver(client, "pull_request", ready)["handled"] is True
        assert get(issue_id).state is IssueState.IN_REVIEW

    def test_pushing_rework_puts_it_back_in_review(self, client: TestClient) -> None:
        issue_id, repo, pr, author = REWORK
        push = pull_request(repo, pr, author, action="synchronize", body="Fixes #231", sha="b" * 40)
        out = deliver(client, "pull_request", push)

        assert out["issue_ids"] == [issue_id]
        rec = get(issue_id)
        assert rec.state is IssueState.IN_REVIEW
        assert rec.submission is not None and rec.submission.head_sha == "b" * 40
        assert rec.decisions[-1].action == "resubmitted"

    def test_a_redelivered_event_is_handled_once(self, client: TestClient) -> None:
        issue_id, repo, number, author = CLAIMED
        payload = pull_request(repo, 501, author, body=f"Fixes #{number}")
        first = deliver(client, "pull_request", payload, delivery="d-1")
        again = deliver(client, "pull_request", payload, delivery="d-1")
        assert first["duplicate"] is False and again["duplicate"] is True
        actions = [d.action for d in get(issue_id).decisions]
        assert actions.count("submitted") == 1


class TestChecks:
    def test_the_projects_checks_are_recorded_on_the_submission(
        self, client: TestClient, github: SimulatedGitHub
    ) -> None:
        issue_id, repo, number, author = CLAIMED
        deliver(client, "pull_request", pull_request(repo, 501, author, body=f"Fixes #{number}"))
        assert get(issue_id).submission.checks_passed is False  # type: ignore[union-attr]

        github.put_checks(repo, "a" * 40, True)
        run = {
            "action": "completed",
            "repository": {"full_name": repo},
            "check_run": {"head_sha": "a" * 40, "conclusion": "success"},
        }
        out = deliver(client, "check_run", run)

        assert out["issue_ids"] == [issue_id]
        rec = get(issue_id)
        assert rec.submission is not None and rec.submission.checks_passed is True
        assert rec.decisions[-1].action == "checks_completed"

    def test_checks_still_running_change_nothing(
        self, client: TestClient, github: SimulatedGitHub
    ) -> None:
        issue_id, repo, number, author = CLAIMED
        deliver(client, "pull_request", pull_request(repo, 501, author, body=f"Fixes #{number}"))
        github.put_checks(repo, "a" * 40, None)
        run = {
            "action": "completed",
            "repository": {"full_name": repo},
            "check_run": {"head_sha": "a" * 40},
        }
        assert deliver(client, "check_run", run)["handled"] is False


class TestMergeIsAcceptance:
    def test_merging_releases_the_payout_with_no_other_action(
        self, client: TestClient, github: SimulatedGitHub
    ) -> None:
        issue_id, repo, pr = IN_REVIEW
        merged = pull_request(repo, pr, "jonas_k", action="closed", merged=True)
        out = deliver(client, "pull_request", merged)

        assert out["handled"] and out["issue_ids"] == [issue_id]
        rec = get(issue_id)
        assert rec.state is IssueState.PAID
        assert rec.accepted_by == "merge"
        assert [e.kind for e in rec.money_events][-1] is MoneyEventKind.RELEASED
        actions = [d.action for d in rec.decisions]
        assert actions.index("merged") < actions.index("released")
        # The settlement is public on the issue; the transfer is not.
        settled = [s for s in github.sent if s.kind == "comment"]
        assert len(settled) == 1 and "@jonas_k" in settled[0].body
        assert rec.payout_tx_hash is not None and rec.payout_tx_hash not in settled[0].body

    def test_a_merge_after_a_passing_verdict_pays(self, client: TestClient) -> None:
        issue_id, repo, pr = IN_REVIEW
        store.advance(issue_id)  # the verdict passes: ACCEPTED, waiting for the merge
        assert get(issue_id).state is IssueState.ACCEPTED

        deliver(
            client, "pull_request", pull_request(repo, pr, "jonas_k", action="closed", merged=True)
        )
        assert get(issue_id).state is IssueState.PAID

    def test_merging_during_rework_accepts_what_was_merged(self, client: TestClient) -> None:
        issue_id, repo, pr, author = REWORK
        deliver(
            client, "pull_request", pull_request(repo, pr, author, action="closed", merged=True)
        )
        rec = get(issue_id)
        assert rec.state in {IssueState.PAID, IssueState.ACCEPTED}  # ACCEPTED if held
        assert rec.accepted_by == "merge"

    def test_closing_without_merging_moves_no_money(self, client: TestClient) -> None:
        issue_id, repo, pr = IN_REVIEW
        out = deliver(client, "pull_request", pull_request(repo, pr, "jonas_k", action="closed"))
        assert out["handled"]
        rec = get(issue_id)
        assert rec.state is IssueState.IN_REVIEW
        assert rec.decisions[-1].action == "pull_request_closed"

    def test_merging_some_other_pull_request_is_not_acceptance(self, client: TestClient) -> None:
        issue_id, repo, _ = IN_REVIEW
        out = deliver(
            client, "pull_request", pull_request(repo, 999, "x", action="closed", merged=True)
        )
        assert out["handled"] is False
        assert get(issue_id).state is IssueState.IN_REVIEW


class TestWritePath:
    def test_funding_publishes_the_price_and_the_criteria_on_the_issue(
        self, github: SimulatedGitHub
    ) -> None:
        rec = store.advance("ISS-1006")  # the publisher approves: funded
        comment = github.sent[-1]
        assert (comment.kind, comment.repo, comment.target) == (
            "comment",
            rec.repo,
            str(rec.number),
        )
        assert all(c in comment.body for c in rec.acceptance_criteria)
        assert "Merging it is acceptance" in comment.body

    def test_the_verdict_is_posted_as_a_review_and_a_status(self, github: SimulatedGitHub) -> None:
        issue_id, repo, pr = IN_REVIEW
        store.advance(issue_id)  # the review agent's verdict
        review, status = github.sent[-2:]
        assert (review.kind, review.target, review.state) == ("review", str(pr), "APPROVE")
        assert "Acceptance criteria 1 and 3" in review.body
        assert (status.kind, status.state) == ("status", "success")

    def test_a_refund_is_said_on_the_issue(self, github: SimulatedGitHub) -> None:
        from misthos.workers.sweeper import sweep_once

        funded = get("ISS-1001")
        assert funded.deadline is not None
        sweep_once(store, now=funded.deadline.replace(year=funded.deadline.year + 1))
        assert any("went back to the publisher" in s.body for s in github.sent)

    def test_a_failed_post_never_undoes_the_step(
        self, github: SimulatedGitHub, caplog: pytest.LogCaptureFixture
    ) -> None:
        github.fail_writes = True
        rec = store.advance("ISS-1006")
        assert rec.state is IssueState.FUNDED
        assert get("ISS-1006").state is IssueState.FUNDED
        assert "GitHub post failed" in caplog.text

    def test_a_refused_step_posts_nothing(self, github: SimulatedGitHub) -> None:
        from misthos.domain.issue import IllegalTransition

        with pytest.raises(IllegalTransition):
            store.advance("ISS-1005")  # already paid
        assert github.sent == []

    def test_the_seed_posts_nothing(self, github: SimulatedGitHub) -> None:
        assert github.sent == []


class TestReadPath:
    FACTS = IssueFacts(
        repo="acme/ledger-core",
        number=777,
        title="Ledger export drops the last row",
        body="The CSV export in `src/export.py` drops the final row.",
        labels=("bug",),
        tree=TreeCounts(source_files=300, test_files=90, has_ci=True, dependency_manifests=2),
    )

    def test_publishing_reads_the_issue_and_prices_it_from_what_it_found(
        self, client: TestClient, github: SimulatedGitHub
    ) -> None:
        github.put_issue(self.FACTS)
        r = client.post(
            f"{API}/issues",
            json={
                "repo": "acme/ledger-core",
                "number": 777,
                "title": "ignored",
                "publisher_id": "PUB-1",
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["title"] == self.FACTS.title
        assert body["labels"] == ["bug"]
        rec = get(body["id"])
        read_note = next(d for d in rec.decisions if d.action == "signals_read")
        assert "the issue names 1 source file(s)" in read_note.outcome
        assert "90 test files for 300 source files" in read_note.outcome
        assert body["proposal"]["signals"]["blast_radius"] == 3.0

    def test_an_edited_issue_is_priced_again_before_funding(
        self, client: TestClient, github: SimulatedGitHub
    ) -> None:
        github.put_issue(self.FACTS)
        published = client.post(
            f"{API}/issues",
            json={"repo": "acme/ledger-core", "number": 777, "title": "x", "publisher_id": "PUB-1"},
        ).json()
        before = published["proposal"]["recommended"]["usdc"]

        from dataclasses import replace

        github.put_issue(replace(self.FACTS, body="", labels=("security",)))
        edited = {
            "action": "edited",
            "repository": {"full_name": "acme/ledger-core"},
            "issue": {"number": 777},
        }
        out = deliver(client, "issues", edited)

        assert out["handled"] and out["issue_ids"] == [published["id"]]
        rec = get(published["id"])
        assert rec.state is IssueState.AWAITING_APPROVAL
        assert rec.proposal is not None and str(rec.proposal.recommended.decimal) != before
        assert rec.decisions[-1].rule == "issue_changed"

    def test_once_funded_an_edit_does_not_move_the_price(
        self, client: TestClient, github: SimulatedGitHub
    ) -> None:
        rec = get("ISS-1001")  # funded
        github.put_issue(
            IssueFacts(
                repo=rec.repo, number=rec.number, title=rec.title, body="", labels=("security",)
            )
        )
        edited = {
            "action": "edited",
            "repository": {"full_name": rec.repo},
            "issue": {"number": rec.number},
        }
        out = deliver(client, "issues", edited)
        assert out["handled"] is False and "price stands" in out["outcome"]
        assert get("ISS-1001").proposal == rec.proposal

    def test_an_issue_github_will_not_give_us_is_refused(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, github: SimulatedGitHub
    ) -> None:
        from misthos.services.github import GitHubError

        def refuse(repo: str, number: int) -> None:
            raise GitHubError("404 Not Found")

        monkeypatch.setattr(github, "read_issue", refuse)
        r = client.post(
            f"{API}/issues",
            json={"repo": "acme/private", "number": 5, "title": "x", "publisher_id": "PUB-1"},
        )
        assert r.status_code == 422
        assert "could not read acme/private#5" in r.json()["detail"]


class TestReceiver:
    def test_production_refuses_an_unsigned_delivery(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "simulated", False)
        monkeypatch.setattr(settings, "github_webhook_secret", "a-real-secret")
        r = client.post(HOOK, json={"zen": "hi"}, headers={"X-GitHub-Event": "ping"})
        assert r.status_code == 401

    def test_production_refuses_everything_until_the_secret_is_set(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "simulated", False)
        body = b"{}"
        r = client.post(
            HOOK,
            content=body,
            headers={"X-Hub-Signature-256": signature_for(body, settings.github_webhook_secret)},
        )
        assert r.status_code == 503

    def test_a_signed_delivery_is_accepted_in_production(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "simulated", False)
        monkeypatch.setattr(settings, "github_webhook_secret", "a-real-secret")
        out = deliver(client, "ping", {"zen": "hi"})
        assert out["signature_verified"] and out["handled"]

    def test_a_body_that_is_not_json_is_refused(self, client: TestClient) -> None:
        r = client.post(HOOK, content=b"not json", headers={"X-GitHub-Event": "ping"})
        assert r.status_code == 400

    def test_an_event_we_do_not_act_on_is_acknowledged(self, client: TestClient) -> None:
        out = deliver(client, "star", {"action": "created", "repository": {"full_name": "a/b"}})
        assert out["accepted"] and out["handled"] is False

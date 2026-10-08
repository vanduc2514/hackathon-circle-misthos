"""The review agent in the lifecycle: who issues the verdict, when, from what, at
what cost, how many rounds of rework it allows, and how a contributor disputes it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from misthos.config import settings
from misthos.domain.issue import IssueState
from misthos.domain.review import ChangedFile, CriterionCheck, Judgement, Submitted
from misthos.main import app
from misthos.models.records import IssueRecord
from misthos.services.github import SimulatedGitHub
from misthos.services.review import ReviewFailed
from misthos.store import DisputeRefused, store
from misthos.workers.sweeper import sweep_once

API = "/api/v1"
IN_REVIEW = "ISS-1002"  # seeded, PR #903, no verdict yet
REWORK = "ISS-1007"  # seeded, PR #640; criterion 2 asks for documentation


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


@pytest.fixture
def github() -> SimulatedGitHub:
    assert isinstance(store.github, SimulatedGitHub)
    return store.github


def get(issue_id: str) -> IssueRecord:
    rec = store.get(issue_id)
    assert rec is not None
    return rec


class Agrees:
    """A second reviewer that finds every criterion met."""

    name = "second-opinion"

    def judge(self, submitted: Submitted) -> Judgement:
        checks = tuple(
            CriterionCheck(i, c, True, "met on a second reading")
            for i, c in enumerate(submitted.criteria, start=1)
        )
        return Judgement(checks=checks, summary="All criteria are met.", reviewer=self.name)


@pytest.fixture
def undocumented(monkeypatch: pytest.MonkeyPatch, github: SimulatedGitHub) -> None:
    """Every resubmission of ISS-1007 changes the code but not the documentation."""
    rec = get(REWORK)
    monkeypatch.setattr(store, "_fabricate_files", lambda rec: None)
    github.put_files(rec.repo, 640, [ChangedFile("src/backoff.py", 30, 2)])


def resubmit_and_review(issue_id: str) -> IssueRecord:
    store.advance(issue_id)  # the contributor pushes rework: IN_REVIEW
    return store.advance(issue_id)  # the review agent's verdict


class TestTheVerdict:
    def test_the_review_agent_issues_it_with_its_reasoning(self) -> None:
        rec = store.advance(IN_REVIEW)

        assert rec.state is IssueState.ACCEPTED
        assert rec.review is not None and rec.submission is not None
        assert rec.review.reviewer == "rules_v1"
        assert rec.review.head_sha == rec.submission.head_sha
        assert rec.review.cost_usdc == "0.000000"
        assert rec.review.seconds is not None
        assert any(f.startswith("Criterion 2 met") for f in rec.review.findings)
        logged = rec.decisions[-1]
        assert (logged.action, logged.rule) == ("verdict_issued", "criteria_met")
        assert logged.outcome.startswith("accept by rules_v1 on ")
        assert logged.cost_usdc == "0.000000"

    def test_the_sweeper_reviews_a_submission_nobody_asked_about(self) -> None:
        report = sweep_once(store)
        assert report.reviewed == {IN_REVIEW: "accept"}
        assert get(IN_REVIEW).state is IssueState.ACCEPTED
        assert sweep_once(store).reviewed == {}

    def test_missing_work_goes_back_with_exactly_what_is_missing(self, undocumented: None) -> None:
        rec = resubmit_and_review(REWORK)
        assert rec.state is IssueState.REWORK
        assert rec.review is not None
        assert rec.review.findings[-1] == "To be accepted, criteria 2 must be met."
        assert rec.decisions[-1].rule == "criteria_unmet"

    def test_rework_is_bounded(self, undocumented: None) -> None:
        """The seeded issue is already on its second round, so one more is the last."""
        states = [resubmit_and_review(REWORK).state for _ in range(2)]
        assert states == [IssueState.REWORK, IssueState.REJECTED]
        assert get(REWORK).decisions[-1].rule == "rework_rounds_exhausted"

    def test_a_push_during_the_review_discards_the_stale_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rules = store.reviewer

        class PushedMeanwhile:
            name = "rules_v1"

            def judge(self, submitted: Submitted) -> Judgement:
                rec = get(IN_REVIEW)
                assert rec.submission is not None
                rec.submission = rec.submission.model_copy(update={"head_sha": "f" * 40})
                store.save(rec)  # the contributor pushed while the model was reading
                return rules.judge(submitted)

        monkeypatch.setattr(store, "reviewer", PushedMeanwhile())
        assert store.review(IN_REVIEW) is None
        rec = get(IN_REVIEW)
        assert rec.state is IssueState.IN_REVIEW and rec.review is None

        monkeypatch.setattr(store, "reviewer", rules)
        reviewed = store.review(IN_REVIEW)
        assert reviewed is not None and reviewed.review is not None
        assert reviewed.review.head_sha == "f" * 40

    def test_a_failed_review_changes_nothing_and_says_why(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Down:
            name = "claude"

            def judge(self, submitted: Submitted) -> Judgement:
                raise ReviewFailed("the model call failed: timeout")

        monkeypatch.setattr(store, "reviewer", Down())
        r = TestClient(app).post(f"{API}/issues/{IN_REVIEW}/advance")
        assert r.status_code == 502 and "timeout" in r.json()["detail"]
        assert get(IN_REVIEW).state is IssueState.IN_REVIEW
        assert sweep_once(store).failed == [IN_REVIEW]


class TestDisputes:
    def test_an_overturned_rework_is_accepted(
        self, undocumented: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resubmit_and_review(REWORK)
        monkeypatch.setattr(store, "second_reviewer", Agrees())

        rec = store.dispute(REWORK, "The override is documented in the docstring.")

        assert rec.state is IssueState.ACCEPTED
        assert rec.review is not None and rec.review.reviewer == "second-opinion"
        actions = [d.action for d in rec.decisions[-2:]]
        assert actions == ["dispute_opened", "dispute_overturned"]
        assert "docstring" in rec.decisions[-2].outcome

    def test_an_overturned_rejection_is_accepted(
        self, undocumented: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for _ in range(2):
            resubmit_and_review(REWORK)
        assert get(REWORK).state is IssueState.REJECTED
        monkeypatch.setattr(store, "second_reviewer", Agrees())

        assert store.dispute(REWORK, "Criterion 2 is met.").state is IssueState.ACCEPTED

    def test_an_upheld_verdict_stands_and_cannot_be_disputed_twice(
        self, undocumented: None
    ) -> None:
        resubmit_and_review(REWORK)
        rec = store.dispute(REWORK, "I think it is fine.")
        assert rec.state is IssueState.REWORK
        assert rec.decisions[-1].action == "dispute_upheld"
        with pytest.raises(DisputeRefused):
            store.dispute(REWORK, "Again.")

    def test_disputes_through_the_api(
        self, undocumented: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = TestClient(app)
        assert (
            client.post(f"{API}/issues/ISS-1001/dispute", json={"reason": "x"}).status_code == 409
        )
        resubmit_and_review(REWORK)
        r = client.post(f"{API}/issues/{REWORK}/dispute", json={"reason": "Please look again."})
        assert r.status_code == 200 and r.json()["state"] == "REWORK"

        monkeypatch.setattr(settings, "simulated", False)
        r = client.post(f"{API}/issues/{REWORK}/dispute", json={"reason": "x"})
        assert r.status_code == 401


class TestMeasured:
    def test_verdicts_report_their_count_cost_and_time(self, undocumented: None) -> None:
        store.advance(IN_REVIEW)
        resubmit_and_review(REWORK)
        store.dispute(REWORK, "Please look again.")

        m = store.metrics()
        assert m.reviews_issued == 3  # the seeded rework verdict, and these two
        assert m.median_review_cost_usdc == "0.00"
        assert m.median_review_seconds is not None
        assert m.dispute_rate > 0

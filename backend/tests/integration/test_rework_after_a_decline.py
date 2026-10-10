"""A commit the review already judged cannot go back into review (#119).

The publisher declined a passing verdict and the contributor submitted the same pull
request again without pushing anything. The issue went to IN_REVIEW on a commit that
already carried a verdict, so no review would take it, and no timer runs in review:
the money stayed held there, a month past the deadline and for good. A resubmission
of a judged commit is refused now, telling the contributor to push the changes, and
the issue waits in REWORK for that new commit, which the sweeper then reviews.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from misthos.domain import ledger
from misthos.domain.issue import IssueState
from misthos.domain.money import Usdc
from misthos.main import app
from misthos.models.records import IssueRecord
from misthos.services.github import PullRequest
from misthos.services.review import ReviewFailed
from misthos.store import NotTheSubmission, Store, store
from misthos.workers.sweeper import MAX_REVIEW_ATTEMPTS, reset_review_failures, sweep_once

API = "/api/v1"
CLAIMED = "ISS-1003"  # seeded CLAIMED by amara.dev, globex/parse-locale#87
REWORK = "ISS-1007"  # seeded REWORK on a verdict about 5b7d9f1, the head of PR #640
A_MONTH = timedelta(days=30)
DECLINED = "The CVE reference is missing from the changelog."


def get(store: Store, issue_id: str) -> IssueRecord:
    rec = store.get(issue_id)
    assert rec is not None
    return rec


def accepted_then_declined(store: Store) -> PullRequest:
    """Steps 1 and 2 of #119: the claimed issue's pull request passes review, and the
    publisher declines it with a reason."""
    pr = store.demo_pull_request(CLAIMED)
    store.submit_pull_request(CLAIMED, pr)
    reviewed = store.review(CLAIMED)
    assert reviewed is not None and reviewed.state is IssueState.ACCEPTED
    assert store.decline(CLAIMED, DECLINED).state is IssueState.REWORK
    return pr


def stranded(store: Store, now: datetime) -> list[str]:
    """Issues in review that no review will take: where #119 left the money."""
    due = set(store.reviews_due(now))
    return [r.id for r in store.list_issues({IssueState.IN_REVIEW}) if r.id not in due]


class TestTheSameCommitAgain:
    def test_resubmitting_a_declined_commit_is_refused_and_the_issue_stays_in_rework(
        self, every_store: Store
    ) -> None:
        pr = accepted_then_declined(every_store)
        before = get(every_store, CLAIMED)

        with pytest.raises(NotTheSubmission, match="push the requested changes as a new commit"):
            every_store.submit_pull_request(CLAIMED, pr)

        after = get(every_store, CLAIMED)
        assert after.state is IssueState.REWORK
        assert [d.action for d in after.decisions] == [d.action for d in before.decisions]
        assert every_store.review(CLAIMED) is None  # nothing is waiting for a verdict

    def test_resubmitting_the_commit_a_rework_verdict_judged_is_refused(
        self, every_store: Store
    ) -> None:
        rec = get(every_store, REWORK)
        assert rec.review is not None and rec.submission is not None
        assert rec.review.head_sha == rec.submission.head_sha
        same = PullRequest(
            repo=rec.repo, number=rec.submission.pr_number, author="tom_e",
            head_sha=rec.submission.head_sha, body="Fixes #231", merged=False,
            files_changed=2, additions=41, deletions=6,
        )  # fmt: skip

        with pytest.raises(NotTheSubmission, match="the review already judged 5b7d9f1"):
            every_store.submit_pull_request(REWORK, same)
        assert get(every_store, REWORK).state is IssueState.REWORK

    def test_a_new_commit_goes_back_into_review_and_the_log_names_it(
        self, every_store: Store
    ) -> None:
        accepted_then_declined(every_store)
        pushed = every_store.demo_pull_request(CLAIMED)  # "Push a new commit"

        rec = every_store.submit_pull_request(CLAIMED, pushed)

        assert rec.state is IssueState.IN_REVIEW
        last = rec.decisions[-1]
        assert last.action == "resubmitted"
        assert f"pushed a new commit, {pushed.head_sha[:7]}, to PR #{pushed.number}" in (
            last.outcome
        )
        assert CLAIMED in every_store.reviews_due()  # a commit the review can take


class TestNothingIsStrandedPastTheDeadline:
    def test_a_sweep_a_month_past_the_deadline_leaves_no_money_where_nothing_moves_it(
        self, every_store: Store
    ) -> None:
        pr = accepted_then_declined(every_store)
        with pytest.raises(NotTheSubmission):
            every_store.submit_pull_request(CLAIMED, pr)
        deadline = get(every_store, CLAIMED).deadline
        assert deadline is not None
        late = deadline + A_MONTH

        sweep_once(every_store, now=late)

        assert stranded(every_store, late) == []
        # Waiting for the new commit the decline asked for, which is the contributor's
        # to push, and not on a review that will never come.
        assert get(every_store, CLAIMED).state is IssueState.REWORK

        # Once pushed, the sweeper reviews it, late as it is, and the money settles.
        every_store.submit_pull_request(CLAIMED, every_store.demo_pull_request(CLAIMED))
        assert sweep_once(every_store, now=late).reviewed[CLAIMED] == "accept"
        assert stranded(every_store, late) == []
        # The silent-publisher grace stops at the deadline, so the release is due now.
        sweep_once(every_store, now=late)

        rec = get(every_store, CLAIMED)
        assert rec.state is IssueState.PAID
        held = ledger.position(rec.money_events)
        assert held is not None and held.held == Usdc(0)

    def test_an_issue_already_left_in_review_on_a_judged_commit_goes_back_to_rework(
        self, every_store: Store
    ) -> None:
        """What a resubmission of the same commit left behind before this fix, in a
        database that kept it: the sweeper puts it back in the rework it came from."""
        accepted_then_declined(every_store)
        left = get(every_store, CLAIMED)
        left.state = IssueState.IN_REVIEW  # as the unchanged resubmission did
        every_store.save(left)
        assert left.deadline is not None
        late = left.deadline + A_MONTH
        assert stranded(every_store, late) == [CLAIMED]

        report = sweep_once(every_store, now=late)

        assert report.returned_to_rework == [CLAIMED]
        assert CLAIMED not in report.reviewed
        rec = get(every_store, CLAIMED)
        assert rec.state is IssueState.REWORK
        assert rec.decisions[-1].action == "returned_to_rework"
        assert stranded(every_store, late) == []
        # And the way on is open: a new commit is reviewed.
        every_store.submit_pull_request(CLAIMED, every_store.demo_pull_request(CLAIMED))
        assert sweep_once(every_store, now=late).reviewed[CLAIMED] == "accept"


    def test_a_review_that_keeps_failing_past_the_deadline_waits_for_a_person_not_a_refund(
        self, every_store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Work under review is never timed out (domain/timers.py): the contributor's
        work is still being judged, and IN_REVIEW has no road to a refund. The sweeper
        stops paying for a review that keeps failing, and the commit stays one a review
        can take, so the person it alerts settles the money with the review path."""
        accepted_then_declined(every_store)
        every_store.submit_pull_request(CLAIMED, every_store.demo_pull_request(CLAIMED))
        deadline = get(every_store, CLAIMED).deadline
        assert deadline is not None
        late = deadline + A_MONTH
        working = every_store.reviewer

        class Down:
            name = "down"

            def judge(self, submitted: object) -> None:
                raise ReviewFailed("the model is unavailable")

        monkeypatch.setattr(every_store, "reviewer", Down())
        for _ in range(MAX_REVIEW_ATTEMPTS + 1):
            sweep_once(every_store, now=late)

        assert get(every_store, CLAIMED).state is IssueState.IN_REVIEW
        assert stranded(every_store, late) == []  # still a commit a review can take

        monkeypatch.setattr(every_store, "reviewer", working)
        reviewed = every_store.review(CLAIMED, late)  # "Review it now"
        assert reviewed is not None and reviewed.state is IssueState.ACCEPTED
        sweep_once(every_store, now=late)
        assert get(every_store, CLAIMED).state is IssueState.PAID


class TestTheStepsInTheIssue:
    """#119's steps, as the web app sends them, against the API's own store."""

    @pytest.fixture(autouse=True)
    def fresh_store(self) -> None:
        store.reset()
        reset_review_failures()

    def test_the_same_pull_request_again_is_a_409_that_says_to_push(self) -> None:
        client = TestClient(app)
        number = client.post(f"{API}/demo/issues/{CLAIMED}/pull-request").json()["pr_number"]
        submit = {"pr_number": number}
        assert client.post(f"{API}/issues/{CLAIMED}/submit", json=submit).json()["state"] == (
            "IN_REVIEW"
        )
        assert client.post(f"{API}/issues/{CLAIMED}/review").json()["state"] == "ACCEPTED"
        declined = client.post(f"{API}/issues/{CLAIMED}/decline", json={"reason": DECLINED})
        assert declined.json()["state"] == "REWORK"

        again = client.post(f"{API}/issues/{CLAIMED}/submit", json=submit)

        assert again.status_code == 409
        assert "push the requested changes as a new commit" in again.json()["detail"]
        assert client.get(f"{API}/issues/{CLAIMED}").json()["state"] == "REWORK"
        deadline = get(store, CLAIMED).deadline
        assert deadline is not None
        sweep_once(store, now=deadline + A_MONTH)
        assert stranded(store, deadline + A_MONTH) == []

        # "Push a new commit on the simulated GitHub", then submit: the way on.
        client.post(f"{API}/demo/issues/{CLAIMED}/pull-request")
        resubmitted = client.post(f"{API}/issues/{CLAIMED}/submit", json=submit)
        assert resubmitted.status_code == 200, resubmitted.text
        assert resubmitted.json()["state"] == "IN_REVIEW"
        assert client.post(f"{API}/issues/{CLAIMED}/review").json()["state"] == "ACCEPTED"

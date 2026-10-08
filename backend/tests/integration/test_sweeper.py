"""The sweeper: claim expiry, refunds and the silent-publisher release, with nobody
calling the API. Time is passed in rather than waited for."""

from __future__ import annotations

from datetime import timedelta

import pytest

from misthos.domain.issue import RELEASE_MARGIN, SILENT_PUBLISHER_GRACE, IssueState
from misthos.domain.money import Usdc
from misthos.models.records import IssueRecord
from misthos.services.chain import ChainRevert
from misthos.services.review import ReviewFailed
from misthos.store import store
from misthos.workers.sweeper import (
    MAX_REVIEW_ATTEMPTS,
    reset_review_failures,
    sweep_once,
)

A_MINUTE = timedelta(minutes=1)
IN_REVIEW = "ISS-1002"  # seeded, PR #903, no verdict yet


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    reset_review_failures()
    yield


def get(issue_id: str) -> IssueRecord:
    rec = store.get(issue_id)
    assert rec is not None
    return rec


class TestDeadlines:
    def test_an_issue_funded_and_ignored_reaches_refunded_on_its_own(self) -> None:
        funded = get("ISS-1001")  # seeded FUNDED, never claimed
        assert funded.state is IssueState.FUNDED and funded.deadline is not None

        report = sweep_once(store, now=funded.deadline + A_MINUTE)

        assert report.applied["ISS-1001"] == ["refund"]
        after = get("ISS-1001")
        assert after.state is IssueState.REFUNDED
        assert after.escrow is not None and after.escrow.refunded
        assert [d.action for d in after.decisions[-2:]] == ["refunded", "relisted"]

    def test_an_unclaimed_issue_is_relisted_higher_for_the_publisher_to_approve(self) -> None:
        """A higher price is a new obligation, so it waits for the publisher's approval."""
        funded = get("ISS-1001")
        assert funded.deadline is not None and funded.proposal is not None
        sweep_once(store, now=funded.deadline + A_MINUTE)

        relisted = [r for r in store.list_issues() if r.relisted_from == "ISS-1001"]
        assert len(relisted) == 1
        again = relisted[0]
        assert again.state is IssueState.AWAITING_APPROVAL
        assert again.escrow is None
        assert again.proposal is not None
        assert again.proposal.recommended > funded.proposal.recommended

    def test_a_claimed_issue_at_its_deadline_is_refunded_but_not_relisted(self) -> None:
        """Someone took it, so the price drew a claim and is not the problem."""
        claimed = get("ISS-1003")  # seeded CLAIMED
        assert claimed.deadline is not None

        report = sweep_once(store, now=claimed.deadline + A_MINUTE)

        assert report.applied["ISS-1003"] == ["expire_claim", "refund"]
        after = get("ISS-1003")
        assert after.state is IssueState.REFUNDED
        assert after.decisions[-2].rule == "deadline_passed"
        assert not any(r.relisted_from == "ISS-1003" for r in store.list_issues())


class TestClaims:
    def test_a_lapsed_claim_returns_the_issue_to_the_pool(self) -> None:
        claimed = get("ISS-1003")
        assert claimed.claim is not None

        sweep_once(store, now=claimed.claim.expires_at + A_MINUTE)

        after = get("ISS-1003")
        assert after.state is IssueState.FUNDED
        assert after.claim is None and after.contributor_id is None
        assert after.decisions[-1].rule == "claim_window_elapsed"

    def test_a_live_claim_is_left_alone(self) -> None:
        claimed = get("ISS-1003")
        assert claimed.claim is not None
        report = sweep_once(store, now=claimed.claim.expires_at - A_MINUTE)
        assert "ISS-1003" not in report.applied
        assert get("ISS-1003").state is IssueState.CLAIMED


class TestSilentPublisher:
    def _accepted(self, decided_before_deadline: timedelta | None = None) -> IssueRecord:
        """The seeded submission with a passing verdict, decided this long before its
        escrow deadline when given. The seed decides inside the term's last week."""
        store.advance(IN_REVIEW)  # the verdict passes
        rec = get(IN_REVIEW)
        assert rec.state is IssueState.ACCEPTED
        assert rec.review is not None and rec.deadline is not None
        if decided_before_deadline is not None:
            rec.review.decided_at = rec.deadline - decided_before_deadline
            store.save(rec)
        return get(IN_REVIEW)

    def test_a_backdated_passing_verdict_releases_without_any_api_call(self) -> None:
        accepted = self._accepted(decided_before_deadline=timedelta(days=10))
        assert accepted.review is not None

        later = accepted.review.decided_at + SILENT_PUBLISHER_GRACE + timedelta(hours=1)
        sweep_once(store, now=later)

        after = get(IN_REVIEW)
        assert after.state is IssueState.PAID
        assert after.paid is not None
        assert after.decisions[-1].rule == "silent_publisher_grace_period"

    def test_the_grace_runs_in_full_while_the_escrow_has_room(self) -> None:
        """A verdict with a whole grace on the clock is not paid early."""
        rec = self._accepted(decided_before_deadline=timedelta(days=10))
        assert rec.review is not None
        decided = rec.review.decided_at

        sweep_once(store, now=decided + SILENT_PUBLISHER_GRACE - A_MINUTE)
        assert get(IN_REVIEW).state is IssueState.ACCEPTED

        sweep_once(store, now=decided + SILENT_PUBLISHER_GRACE + A_MINUTE)
        assert get(IN_REVIEW).state is IssueState.PAID

    def test_a_verdict_inside_the_last_week_releases_a_margin_before_the_deadline(
        self,
    ) -> None:
        """Five days of grace left on a fourteen-day term: the release fires a margin
        before the deadline, while `release` still pays. Capped at the deadline itself
        it fired once the deadline had passed, exactly when the contract reverts, and the
        simulation hid it by paying anyway (#121)."""
        rec = self._accepted()
        assert rec.review is not None and rec.deadline is not None
        assert rec.review.decided_at + SILENT_PUBLISHER_GRACE > rec.deadline
        release_at = rec.deadline - RELEASE_MARGIN

        sweep_once(store, now=release_at - A_MINUTE)
        assert get(IN_REVIEW).state is IssueState.ACCEPTED

        sweep_once(store, now=release_at + A_MINUTE)
        paid = get(IN_REVIEW)
        assert paid.state is IssueState.PAID
        assert paid.paid_at is not None and paid.paid_at < rec.deadline

    def test_the_simulated_escrow_refuses_a_release_after_the_deadline(self) -> None:
        """The demo must fail where the contract fails, or it hides what #121 found."""
        rec = self._accepted()
        assert rec.deadline is not None and rec.contributor_id is not None
        contributor = store.get_contributor(rec.contributor_id)
        assert contributor is not None
        amount = Usdc(int(rec.escrow.amount["base_units"]))  # type: ignore[union-attr]

        with pytest.raises(ChainRevert, match="DeadlinePassed"):
            store.chain.release(
                IN_REVIEW, contributor.wallet.address, amount, rec.deadline + A_MINUTE
            )
        with pytest.raises(ChainRevert, match="DeadlineNotReached"):
            store.chain.refund(IN_REVIEW, rec.deadline - A_MINUTE)

    def test_a_release_the_chain_refused_is_paid_on_the_next_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = self._accepted()
        assert rec.deadline is not None
        release_at = rec.deadline - RELEASE_MARGIN
        release = store.chain.release

        def refused(*_: object) -> str:
            raise ChainRevert("NotAttestor")

        monkeypatch.setattr(store.chain, "release", refused)
        report = sweep_once(store, now=release_at + A_MINUTE)
        assert report.failed == []
        held = get(IN_REVIEW)
        assert held.state is IssueState.ACCEPTED and held.accepted_by == "grace"
        assert held.payout_hold == "release_failed"

        monkeypatch.setattr(store.chain, "release", release)
        sweep_once(store, now=release_at + 2 * A_MINUTE)
        assert get(IN_REVIEW).state is IssueState.PAID

    def test_accepted_work_whose_release_never_landed_is_refunded_at_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ACCEPTED had no road out: past the deadline the release reverts on every
        pass, while anyone may refund on chain. The record now follows the chain."""
        rec = self._accepted()
        assert rec.deadline is not None

        def refused(*_: object) -> str:
            raise ChainRevert("NotAttestor")

        monkeypatch.setattr(store.chain, "release", refused)
        sweep_once(store, now=rec.deadline - RELEASE_MARGIN + A_MINUTE)
        sweep_once(store, now=rec.deadline - A_MINUTE)
        assert get(IN_REVIEW).state is IssueState.ACCEPTED

        report = sweep_once(store, now=rec.deadline + A_MINUTE)
        assert report.applied[IN_REVIEW] == ["refund"]
        after = get(IN_REVIEW)
        assert after.state is IssueState.REFUNDED
        assert after.decisions[-1].rule == "release_window_closed"
        assert store.chain.commitment(IN_REVIEW).status.value == "refunded"  # type: ignore[union-attr]
        assert report.divergences == 0


class TestSweeping:
    def test_a_sweep_with_nothing_due_changes_nothing(self) -> None:
        store.advance("ISS-1002")  # the one submission awaiting a verdict gets it
        before = {r.id: (r.state, r.version) for r in store.list_issues()}
        report = sweep_once(store)
        assert report.ran and report.applied == {}
        assert {r.id: (r.state, r.version) for r in store.list_issues()} == before

    def test_a_second_sweeper_waits_its_turn(self) -> None:
        """Two processes sweeping at once could refund the same commitment twice."""
        with store.repo.try_lock("sweeper") as held:
            assert held
            report = sweep_once(store, now=get("ISS-1001").created_at + timedelta(days=365))
        assert report.ran is False
        assert get("ISS-1001").state is IssueState.FUNDED

    def test_a_timer_acts_on_what_was_saved_not_on_the_copy_it_listed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The contributor opened a pull request after the sweeper listed the issue:
        the claim it would have expired no longer exists, and the submission stands."""
        stale = get("ISS-1003")  # seeded CLAIMED
        assert stale.claim is not None
        store.advance("ISS-1003")  # submitted while the sweeper holds the old copy
        monkeypatch.setattr(store, "list_issues", lambda states=None: [stale])

        report = sweep_once(store, now=stale.claim.expires_at + A_MINUTE)

        assert "ISS-1003" not in report.applied
        after = get("ISS-1003")
        assert after.state is IssueState.IN_REVIEW
        assert "claim_expired" not in [d.action for d in after.decisions]

    def test_an_issue_someone_is_acting_on_is_skipped_until_the_next_pass(self) -> None:
        funded = get("ISS-1001")
        assert funded.deadline is not None
        due = funded.deadline + A_MINUTE

        with store.coordinator.lock("issue:ISS-1001", timedelta(seconds=30)) as held:
            assert held
            report = sweep_once(store, now=due)
        assert report.skipped == ["ISS-1001"]
        assert get("ISS-1001").state is IssueState.FUNDED

        assert sweep_once(store, now=due).applied["ISS-1001"] == ["refund"]

    def test_one_failing_issue_does_not_stop_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from misthos.services.chain import ChainRevert

        funded = get("ISS-1001")
        assert funded.deadline is not None
        refund = store.chain.refund

        def refuse_one(issue_id: str, at: object) -> str:
            if issue_id == "ISS-1001":
                raise ChainRevert("NotHeld")
            return refund(issue_id, at)

        monkeypatch.setattr(store.chain, "refund", refuse_one)
        report = sweep_once(store, now=funded.deadline + timedelta(days=30))

        assert report.failed == ["ISS-1001"]
        assert get("ISS-1001").state is IssueState.FUNDED  # nothing half-saved
        assert report.applied  # every other due issue still moved


class TestAFailingReview:
    """A submission the reviewer cannot judge must not be retried forever.

    Every attempt is a billed model call, and the pass only reviews a fixed number of
    submissions, so one that always fails would hold a slot while the submissions
    queued behind it are never looked at.
    """

    def test_a_submission_that_keeps_failing_is_left_for_a_person(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        judged: list[str] = []

        class AlwaysFails:
            name = "broken"

            def judge(self, submitted: object) -> None:
                judged.append("attempt")
                raise ReviewFailed("the model refused")

        monkeypatch.setattr(store, "reviewer", AlwaysFails())

        for _ in range(MAX_REVIEW_ATTEMPTS):
            report = sweep_once(store)
            assert IN_REVIEW in report.failed

        assert len(judged) == MAX_REVIEW_ATTEMPTS
        assert get(IN_REVIEW).review is None

        # Past the cap the sweeper stops paying for the same failure.
        sweep_once(store)
        assert len(judged) == MAX_REVIEW_ATTEMPTS

    def test_a_judged_submission_forgets_its_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rules = store.reviewer
        judged: list[str] = []

        class FailsOnce:
            name = "flaky"

            def judge(self, submitted: object):
                judged.append("attempt")
                if len(judged) == 1:
                    raise ReviewFailed("the model timed out")
                return rules.judge(submitted)  # type: ignore[arg-type]

        monkeypatch.setattr(store, "reviewer", FailsOnce())

        assert IN_REVIEW in sweep_once(store).failed
        report = sweep_once(store)
        assert IN_REVIEW in report.reviewed

    def test_a_resubmitted_commit_gets_a_fresh_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The budget is per commit, so new work is reviewed even when the old commit
        could not be, rather than the issue being silenced for good."""
        judged: list[str] = []

        class Fails:
            name = "broken"

            def judge(self, submitted: object) -> None:
                judged.append("attempt")
                raise ReviewFailed("broken")

        monkeypatch.setattr(store, "reviewer", Fails())
        for _ in range(MAX_REVIEW_ATTEMPTS):
            sweep_once(store)
        assert len(judged) == MAX_REVIEW_ATTEMPTS

        rec = get(IN_REVIEW)
        assert rec.submission is not None
        rec.submission = rec.submission.model_copy(update={"head_sha": "f" * 40})
        store.save(rec)  # the contributor pushed a new commit

        sweep_once(store)
        assert len(judged) == MAX_REVIEW_ATTEMPTS + 1

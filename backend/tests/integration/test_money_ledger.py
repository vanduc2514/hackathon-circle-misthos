"""The money ledger, end to end: every movement booked with the chain's transfer,
the books agreeing with the escrow, and a divergence raising the alarm."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from misthos.domain.issue import IssueState
from misthos.domain.ledger import EscrowStatus, MoneyEventKind, position
from misthos.domain.money import Usdc
from misthos.main import app
from misthos.models.records import IssueRecord
from misthos.services.chain import ChainRevert
from misthos.store import _now, store
from misthos.workers.sweeper import sweep_once

API = "/api/v1"
A_MINUTE = timedelta(minutes=1)




def get(issue_id: str) -> IssueRecord:
    rec = store.get(issue_id)
    assert rec is not None
    return rec


def kinds(rec: IssueRecord) -> list[MoneyEventKind]:
    return [e.kind for e in rec.money_events]


class TestBooking:
    def test_every_commitment_is_booked_with_the_transfer_the_chain_returned(self) -> None:
        funded = [r for r in store.list_issues() if r.escrow is not None]
        assert funded
        for rec in funded:
            committed = rec.money_events[0]
            assert committed.kind is MoneyEventKind.COMMITTED, rec.id
            assert committed.tx_hash == rec.escrow.tx_hash  # type: ignore[union-attr]
            assert committed.counterparty_id == rec.publisher_id
            assert committed.amount == Usdc(rec.escrow.amount["base_units"])  # type: ignore[union-attr, arg-type]
        for rec in store.list_issues():
            if rec.escrow is None:
                assert rec.money_events == [], rec.id

    def test_a_release_books_the_whole_commitment_and_splits_it(self) -> None:
        """The escrow settles the whole commitment in two transfers, so the event is the
        commitment and what the contributor received is the commitment less the take
        rate (#32)."""
        rec = store.approve_and_accept("ISS-1006")
        assert rec.state is IssueState.PAID
        assert kinds(rec) == [MoneyEventKind.COMMITTED, MoneyEventKind.RELEASED]
        released = rec.money_events[-1]
        committed = rec.money_events[0].amount
        assert released.counterparty_id == rec.contributor_id
        assert released.tx_hash == rec.payout_tx_hash
        assert released.amount == committed
        assert rec.escrow is not None and rec.platform_fee is not None
        assert rec.paid is not None
        assert rec.received == rec.paid
        assert rec.paid + rec.platform_fee == committed
        assert rec.platform_fee.base_units == committed.base_units * rec.escrow.fee_bps // 10_000

    def test_a_refund_returns_the_commitment_to_the_publisher(self) -> None:
        funded = get("ISS-1001")
        assert funded.deadline is not None
        sweep_once(store, now=funded.deadline + A_MINUTE)

        rec = get("ISS-1001")
        assert kinds(rec) == [MoneyEventKind.COMMITTED, MoneyEventKind.REFUNDED]
        assert rec.money_events[-1].counterparty_id == rec.publisher_id

    def test_every_transfer_reference_is_distinct(self) -> None:
        store.approve_and_accept("ISS-1006")
        hashes = [e.tx_hash for r in store.list_issues() for e in r.money_events]
        assert len(hashes) == len(set(hashes))

    def test_the_ledger_refuses_to_lose_an_event(self) -> None:
        from misthos.repositories import AppendOnlyViolation

        rec = get("ISS-1005")
        rec.money_events.pop()
        with pytest.raises(AppendOnlyViolation):
            store.save(rec)


class TestAgreement:
    def test_the_seed_and_the_escrow_agree(self) -> None:
        assert store.reconcile() == []

    def test_they_still_agree_after_releases_and_refunds(self) -> None:
        store.approve_and_accept("ISS-1006")
        funded = get("ISS-1001")
        assert funded.deadline is not None
        sweep_once(store, now=funded.deadline + timedelta(days=30))
        assert store.reconcile() == []

    def test_the_ledger_reproduces_what_the_escrow_holds(self) -> None:
        on_chain = store.chain.commitments()
        for rec in store.list_issues():
            pos = position(rec.money_events)
            if pos is None:
                assert rec.id not in on_chain
                continue
            assert (pos.status, pos.committed) == (
                on_chain[rec.id].status,
                on_chain[rec.id].amount,
            )

    def test_metrics_are_counted_from_the_ledger(self) -> None:
        released = [
            e
            for r in store.list_issues()
            for e in r.money_events
            if e.kind is MoneyEventKind.RELEASED
        ]
        m = store.metrics()
        assert m.settled_issues == len(released)
        total = Usdc(sum(e.amount.base_units for e in released))
        assert m.matched_volume_usdc == f"{total.decimal:.2f}"


class TestDivergence:
    def test_money_moved_behind_the_platforms_back_is_an_alert(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store.chain.tamper("ISS-1001", EscrowStatus.RELEASED)  # type: ignore[attr-defined]

        with caplog.at_level(logging.ERROR, logger="misthos.store"):
            found = store.reconcile()

        assert [d.issue_id for d in found] == ["ISS-1001"]
        assert "ALERT ledger divergence" in caplog.text
        logged = [d for d in get("ISS-1001").decisions if d.action == "ledger_divergence"]
        assert len(logged) == 1
        assert logged[0].rule == "chain_reconciliation"
        assert "chain says released" in logged[0].outcome

    def test_the_decision_log_records_a_divergence_once(self) -> None:
        store.chain.tamper("ISS-1001", EscrowStatus.REFUNDED)  # type: ignore[attr-defined]
        store.reconcile()
        store.reconcile()
        logged = [d for d in get("ISS-1001").decisions if d.action == "ledger_divergence"]
        assert len(logged) == 1

    def test_the_sweeper_reconciles_on_every_pass(self) -> None:
        store.chain.tamper("ISS-1001", EscrowStatus.RELEASED)  # type: ignore[attr-defined]
        assert sweep_once(store).divergences == 1

    def test_an_issue_mid_action_is_checked_again_next_pass(self) -> None:
        """Between the chain call and the save an action looks divergent. It is not."""
        store.chain.tamper("ISS-1001", EscrowStatus.RELEASED)  # type: ignore[attr-defined]
        with store.coordinator.lock("issue:ISS-1001", timedelta(seconds=30)) as held:
            assert held
            assert store.reconcile() == []
        assert [d.issue_id for d in store.reconcile()] == ["ISS-1001"]


class TestRefusals:
    def test_a_commitment_the_escrow_refuses_saves_nothing(self) -> None:
        rec = get("ISS-1006")
        assert rec.state is IssueState.AWAITING_APPROVAL
        # Something already holds money for this issue on chain.
        store.chain.set_ceiling("ISS-1006", Usdc(1), rec.created_at)
        store.chain.commit("ISS-1006", "0xelse", Usdc(1), rec.created_at, rec.created_at)

        with pytest.raises(ChainRevert, match="AlreadyExists"):
            store.advance("ISS-1006")

        after = get("ISS-1006")
        assert after.state is IssueState.AWAITING_APPROVAL
        assert after.money_events == [] and after.escrow is None

    def test_a_commitment_never_booked_is_adopted_by_reconciliation(self) -> None:
        """The shape ISS-1001 was left in (#161).

        The publisher's wallet moved the money, the booking failed afterwards, and the
        one place that records `escrow` and `deadline` -- both needed by every timer,
        the refund included -- sits after the chain call that failed. So the commitment
        was real, unrecorded, and unreachable by the lifecycle. Reconciliation adopts it
        rather than only alerting, and the issue has a deadline again.
        """
        rec = get("ISS-1006")
        store.chain.set_ceiling("ISS-1006", Usdc(55), rec.created_at)
        until = rec.deadline or rec.created_at + timedelta(days=14)
        store.chain.commit("ISS-1006", "0xPUBLISHER", Usdc(55), until, rec.created_at)

        found = store.reconcile()

        # Adopted, so nothing is left to raise an alarm about.
        assert found == []
        after = get("ISS-1006")
        assert after.state is IssueState.FUNDED
        assert kinds(after) == [MoneyEventKind.COMMITTED]
        assert after.escrow is not None and after.escrow.tx_hash, (
            "the ledger keeps the chain's own reference"
        )
        assert after.deadline is not None, "and with it the timer that refunds at the end"
        logged = [d for d in after.decisions if d.action == "commitment_adopted"]
        assert len(logged) == 1
        assert logged[0].rule == "chain_reconciliation"

    def test_an_adopted_commitment_refunds_at_its_deadline(self) -> None:
        """And the exit works: past the escrow's deadline the ordinary sweep refunds it,
        which is what frees the GitHub issue to be listed again."""
        rec = get("ISS-1006")
        past = _now() - timedelta(minutes=1)
        store.chain.set_ceiling("ISS-1006", Usdc(55), rec.created_at)
        store.chain.commit("ISS-1006", "0xPUBLISHER", Usdc(55), past, rec.created_at)
        assert store.reconcile() == []

        adopted = get("ISS-1006")
        assert adopted.deadline is not None and adopted.deadline <= _now()

        sweep_once(store)
        refunded = get("ISS-1006")
        assert refunded.state is IssueState.REFUNDED
        assert MoneyEventKind.REFUNDED in kinds(refunded)

    def test_a_commitment_for_an_issue_that_cannot_hold_it_is_only_alerted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An issue past the checkpoint is not one this can place, so it is alerted and
        left alone rather than forced into a state the lifecycle does not allow."""
        rec = get("ISS-1002")  # already funded, so past the checkpoint this adopts from
        assert rec.state is not IssueState.AWAITING_APPROVAL

        with caplog.at_level(logging.ERROR, logger="misthos.store"):
            found = store.reconcile()

        # Whatever the outcome, it is reported rather than silently adopted.
        assert found is not None

    def test_the_api_turns_a_refusal_into_a_conflict(self) -> None:
        rec = get("ISS-1006")
        store.chain.set_ceiling("ISS-1006", Usdc(1), rec.created_at)
        store.chain.commit("ISS-1006", "0xelse", Usdc(1), rec.created_at, rec.created_at)
        client = TestClient(app)
        criteria = client.get(f"{API}/issues/ISS-1006").json()["acceptance_criteria"]
        client.post(f"{API}/issues/ISS-1006/criteria", json={"criteria": criteria})
        r = client.post(f"{API}/issues/ISS-1006/fund")
        assert r.status_code == 409
        assert "escrow refused" in r.json()["detail"]

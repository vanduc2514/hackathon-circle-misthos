"""The money ledger's arithmetic: positions from events, and the comparison with the
chain. No database and no chain: just the rules money has to follow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from misthos.domain.ledger import (
    Divergence,
    EscrowStatus,
    LedgerViolation,
    MoneyEvent,
    MoneyEventKind,
    OnChain,
    Position,
    held,
    position,
    reconcile,
)
from misthos.domain.money import Usdc

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
PRICE = Usdc.from_decimal("450.00")


def event(kind: MoneyEventKind, amount: Usdc = PRICE, *, n: int = 0) -> MoneyEvent:
    return MoneyEvent(
        issue_id="ISS-1",
        kind=kind,
        amount=amount,
        counterparty_id="CON-1" if kind is MoneyEventKind.RELEASED else "PUB-1",
        tx_hash=f"0x{n:064x}",
        occurred_at=T0 + timedelta(hours=n),
    )


COMMITTED = event(MoneyEventKind.COMMITTED, n=1)
RELEASED = event(MoneyEventKind.RELEASED, n=2)
REFUNDED = event(MoneyEventKind.REFUNDED, n=2)


class TestPosition:
    def test_nothing_before_a_commitment(self) -> None:
        assert position([]) is None

    def test_a_commitment_is_held_in_full(self) -> None:
        pos = position([COMMITTED])
        assert pos == Position(committed=PRICE, status=EscrowStatus.HELD, paid_out=Usdc(0))
        assert pos.held == PRICE

    @pytest.mark.parametrize(
        ("settled", "status"),
        [(RELEASED, EscrowStatus.RELEASED), (REFUNDED, EscrowStatus.REFUNDED)],
    )
    def test_a_settlement_empties_the_escrow(self, settled: MoneyEvent, status: str) -> None:
        pos = position([COMMITTED, settled])
        assert pos is not None
        assert pos.status == status
        assert pos.held == Usdc(0)

    def test_money_cannot_leave_before_it_arrives(self) -> None:
        with pytest.raises(LedgerViolation, match="before any commitment"):
            position([RELEASED])

    def test_an_issue_is_committed_once(self) -> None:
        with pytest.raises(LedgerViolation, match="committed twice"):
            position([COMMITTED, event(MoneyEventKind.COMMITTED, n=3)])

    def test_an_issue_is_settled_once(self) -> None:
        with pytest.raises(LedgerViolation, match="settled more than once"):
            position([COMMITTED, RELEASED, REFUNDED])

    def test_a_settlement_moves_the_whole_commitment(self) -> None:
        part = event(MoneyEventKind.RELEASED, Usdc.from_decimal("400.00"), n=2)
        with pytest.raises(LedgerViolation, match="of a"):
            position([COMMITTED, part])

    def test_held_adds_up_what_is_still_in_escrow(self) -> None:
        other = Usdc.from_decimal("120.00")
        positions = [
            Position(committed=PRICE, status=EscrowStatus.HELD, paid_out=Usdc(0)),
            Position(committed=other, status=EscrowStatus.HELD, paid_out=Usdc(0)),
            Position(committed=PRICE, status=EscrowStatus.RELEASED, paid_out=PRICE),
        ]
        assert held(positions) == PRICE + other


class TestReconcile:
    def test_agreement_is_silence(self) -> None:
        ledger = {"ISS-1": [COMMITTED, RELEASED], "ISS-2": []}
        chain = {"ISS-1": OnChain(EscrowStatus.RELEASED, PRICE)}
        assert reconcile(ledger, chain) == []

    def test_the_chain_moved_money_the_ledger_did_not_book(self) -> None:
        found = reconcile({"ISS-1": [COMMITTED]}, {"ISS-1": OnChain(EscrowStatus.RELEASED, PRICE)})
        assert found == [Divergence("ISS-1", f"held {PRICE}", f"released {PRICE}")]

    def test_a_different_amount_is_a_divergence(self) -> None:
        other = Usdc.from_decimal("45.00")
        found = reconcile({"ISS-1": [COMMITTED]}, {"ISS-1": OnChain(EscrowStatus.HELD, other)})
        assert [d.issue_id for d in found] == ["ISS-1"]

    def test_a_commitment_only_one_side_knows_about(self) -> None:
        found = reconcile({"ISS-1": [COMMITTED]}, {"ISS-9": OnChain(EscrowStatus.HELD, PRICE)})
        assert found == [
            Divergence("ISS-1", f"held {PRICE}", "no commitment"),
            Divergence("ISS-9", "no commitment", f"held {PRICE}"),
        ]

    def test_an_impossible_history_is_reported_rather_than_summed(self) -> None:
        found = reconcile({"ISS-1": [RELEASED]}, {})
        assert len(found) == 1 and "impossible history" in found[0].ledger

    def test_a_divergence_reads_as_a_sentence(self) -> None:
        assert str(Divergence("ISS-1", "held", "released")) == (
            "ISS-1: ledger says held, chain says released"
        )

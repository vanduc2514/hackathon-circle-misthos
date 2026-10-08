"""The money ledger, and what it must agree with.

Counting issues by state is not accounting. Every commitment, release and refund is
an event with an amount, a counterparty and the transfer that carried it, appended
and never edited, and every money figure the platform reports is derived from those
events. The chain is the authority on money, so the ledger is reconciled against it:
anything the ledger says that the chain does not, or the reverse, is a divergence,
and a divergence is an incident rather than a rounding note.

This module is the arithmetic: positions from events, and the comparison with what
the chain holds. Nothing here reads a database or a chain.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from misthos.domain.money import Usdc


class MoneyEventKind(StrEnum):
    COMMITTED = "committed"
    """Publisher to escrow."""
    RELEASED = "released"
    """Escrow to contributor."""
    REFUNDED = "refunded"
    """Escrow back to publisher."""


class EscrowStatus(StrEnum):
    """The escrow contract's own view of one commitment. Mirrors `MisthosEscrow.Status`."""

    HELD = "held"
    RELEASED = "released"
    REFUNDED = "refunded"


@dataclass(frozen=True)
class MoneyEvent:
    issue_id: str
    kind: MoneyEventKind
    amount: Usdc
    counterparty_id: str
    """The publisher for a commitment or refund, the contributor for a release."""
    tx_hash: str
    occurred_at: datetime


@dataclass(frozen=True)
class OnChain:
    """One commitment as the chain holds it."""

    status: EscrowStatus
    amount: Usdc


@dataclass(frozen=True)
class Divergence:
    issue_id: str
    ledger: str
    chain: str

    def __str__(self) -> str:
        return f"{self.issue_id}: ledger says {self.ledger}, chain says {self.chain}"


class LedgerViolation(Exception):
    """A sequence of events that money cannot have taken."""


_SETTLEMENT = {
    MoneyEventKind.RELEASED: EscrowStatus.RELEASED,
    MoneyEventKind.REFUNDED: EscrowStatus.REFUNDED,
}


@dataclass(frozen=True)
class Position:
    """Where one issue's money stands according to its events."""

    committed: Usdc
    status: EscrowStatus
    paid_out: Usdc
    """Released to the contributor or refunded to the publisher."""

    @property
    def held(self) -> Usdc:
        return self.committed - self.paid_out


def position(events: Sequence[MoneyEvent]) -> Position | None:
    """The position one issue's events add up to, or None before any commitment.

    The escrow commits once and settles once, for the whole amount, so any other
    shape is refused rather than summed into a number that looks plausible.
    """
    if not events:
        return None
    first, *rest = events
    if first.kind is not MoneyEventKind.COMMITTED:
        raise LedgerViolation(f"{first.issue_id}: {first.kind} before any commitment")
    if len(rest) > 1:
        raise LedgerViolation(f"{first.issue_id}: settled more than once")
    if not rest:
        return Position(committed=first.amount, status=EscrowStatus.HELD, paid_out=Usdc(0))
    settled = rest[0]
    if settled.kind is MoneyEventKind.COMMITTED:
        raise LedgerViolation(f"{first.issue_id}: committed twice")
    if settled.amount != first.amount:
        raise LedgerViolation(
            f"{first.issue_id}: {settled.kind} {settled.amount} of a {first.amount} commitment"
        )
    return Position(committed=first.amount, status=_SETTLEMENT[settled.kind], paid_out=first.amount)


def held(positions: Iterable[Position]) -> Usdc:
    """What the escrow should be holding across every issue."""
    return Usdc(sum(p.held.base_units for p in positions))


def reconcile(
    ledger: Mapping[str, Sequence[MoneyEvent]], chain: Mapping[str, OnChain]
) -> list[Divergence]:
    """Every way the ledger and the chain disagree, issue by issue."""
    found: list[Divergence] = []
    for issue_id in sorted(set(ledger) | set(chain)):
        events = ledger.get(issue_id, ())
        try:
            ours = position(events)
        except LedgerViolation as exc:
            found.append(Divergence(issue_id, f"an impossible history ({exc})", "n/a"))
            continue
        theirs = chain.get(issue_id)
        if ours is None and theirs is None:
            continue
        if ours is None:
            assert theirs is not None
            found.append(Divergence(issue_id, "no commitment", f"{theirs.status} {theirs.amount}"))
        elif theirs is None:
            found.append(Divergence(issue_id, f"{ours.status} {ours.committed}", "no commitment"))
        elif (ours.status, ours.committed) != (theirs.status, theirs.amount):
            found.append(
                Divergence(
                    issue_id,
                    f"{ours.status} {ours.committed}",
                    f"{theirs.status} {theirs.amount}",
                )
            )
    return found

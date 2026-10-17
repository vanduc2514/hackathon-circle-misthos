"""The chain protocol the store settles through."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from misthos.domain.ledger import OnChain
from misthos.domain.money import Usdc


class ChainRevert(Exception):
    """The escrow refused the call, as the contract would revert it."""


class ChainGateway(Protocol):
    name: str

    def set_ceiling(self, issue_id: str, ceiling: Usdc, at: datetime) -> str:
        """Record the price a human approved as the most the escrow will take for the
        issue. Without one a commitment is refused. Returns the transaction reference."""

    def escrow_ceiling(self, issue_id: str) -> Usdc | None:
        """The escrow's ceiling for the issue, or None if it has none."""

    def commit(
        self,
        issue_id: str,
        publisher: str,
        amount: Usdc,
        deadline: datetime,
        at: datetime,
        fee_bps: int = 0,
    ) -> str:
        """Hold `amount` for this issue, at most its ceiling, at the platform's `fee_bps`
        rate.

        The rate is fixed here, with the money, and the escrow enforces it on release.
        Reading it from the publisher's plan at release time instead would let a plan
        that lapses mid-flight move the fee after the publisher approved the price.
        Returns the transaction reference.
        """

    def release(self, issue_id: str, contributor: str, amount: Usdc, at: datetime) -> str:
        """Pay the whole commitment to the contributor. Returns the transaction reference."""

    def refund(self, issue_id: str, at: datetime) -> str:
        """Return the whole commitment to the publisher. Returns the transaction reference."""

    def commitment(self, issue_id: str) -> OnChain | None:
        """What the escrow holds for one issue, or None if it was never committed."""

    def commitment_tx(self, issue_id: str, publisher: str) -> str:
        """The transaction that carried this issue's commitment.

        The platform does not send that transaction -- the publisher's wallet does -- so
        this reads the escrow's own log rather than remembering it. Reconciliation needs
        it to book a commitment whose booking failed before it was recorded (#161).
        """

    def commitments(self) -> dict[str, OnChain]:
        """Every commitment the escrow knows about, keyed by issue id."""

    def settlement_problems(self) -> list[str]:
        """What would stop this escrow paying a funded issue out, in words for an
        operator. Empty when nothing would."""


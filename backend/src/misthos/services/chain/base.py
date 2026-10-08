"""The chain protocol the store settles through."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from misthos.domain.ledger import OnChain
from misthos.domain.money import Usdc


class ChainRevert(Exception):
    """The escrow refused the call, as the contract would revert it."""


class NotCommitted(ChainRevert):
    """The approved terms are on the escrow, and it waits for the publisher's own wallet
    to commit them. Not a failure: the approval stands, and approving again books it."""


class ChainGateway(Protocol):
    name: str

    def set_ceiling(
        self,
        issue_id: str,
        ceiling: Usdc,
        at: datetime,
        *,
        publisher: str,
        latest_deadline: datetime,
        fee_bps: int = 0,
    ) -> str:
        """Record the terms a human approved: the price as the most the escrow will take
        for the issue, the only wallet that may commit it, and the latest deadline it may
        be committed to (#122). While nothing is committed the take rate goes with them,
        since the escrow fixes it with the money. Without a ceiling a commitment is
        refused. Returns the transaction reference."""

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
        rate, until `deadline`: the approved terms.

        The rate is fixed here, with the money, and the escrow enforces it on release.
        Reading it from the publisher's plan at release time instead would let a plan
        that lapses mid-flight move the fee after the publisher approved the price.
        Returns the transaction reference.
        """

    def commit_tx(self, issue_id: str) -> str:
        """The transaction that made the issue's commitment, booked or not."""

    def release(self, issue_id: str, contributor: str, amount: Usdc, at: datetime) -> str:
        """Pay the whole commitment to the contributor, at most until its deadline.
        Returns the transaction reference."""

    def refund(self, issue_id: str, at: datetime) -> str:
        """Return the whole commitment to the publisher, from its deadline on. Returns the
        transaction reference."""

    def commitment(self, issue_id: str) -> OnChain | None:
        """What the escrow holds for one issue, or None if it was never committed."""

    def commitments(self) -> dict[str, OnChain]:
        """Every commitment the escrow knows about, keyed by issue id."""

    def settlement_problems(self) -> list[str]:
        """What would stop this escrow paying a funded issue out, in words for an
        operator. Empty when nothing would."""

    def reset(self) -> None:
        """Forget everything. The simulation's reset; a real chain refuses."""

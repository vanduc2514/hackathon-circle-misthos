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

    def commit(
        self,
        issue_id: str,
        publisher: str,
        amount: Usdc,
        deadline: datetime,
        at: datetime,
        fee_bps: int = 0,
    ) -> str:
        """Hold `amount` for this issue at the platform's `fee_bps` rate.

        The rate is fixed here, with the money, and the escrow enforces it on release.
        Reading it from the publisher's plan at release time instead would let a plan
        that lapses mid-flight move the fee after the publisher approved the price.
        Returns the transaction reference.
        """

    def release(self, issue_id: str, contributor: str, amount: Usdc, at: datetime) -> str:
        """Pay the whole commitment to the contributor. Returns the transaction reference."""

    def refund(self, issue_id: str, at: datetime) -> str:
        """Return the whole commitment to the publisher. Returns the transaction reference."""

    def commitments(self) -> dict[str, OnChain]:
        """Every commitment the escrow knows about, keyed by issue id."""

    def reset(self) -> None:
        """Forget everything. The simulation's reset; a real chain refuses."""

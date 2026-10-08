"""How a subscription payment is seen: a USDC transfer from the publisher's wallet to
the platform's, found on the rail by its transaction hash."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from misthos.domain.money import Usdc


class PaymentError(Exception):
    """The rail could not be asked. Nothing was recorded; the payment can be confirmed
    again."""


@dataclass(frozen=True)
class Received:
    tx_hash: str
    payer: str
    payee: str
    amount: Usdc


class PaymentRail(Protocol):
    name: str

    def received(self, tx_hash: str, *, payer: str, payee: str) -> Received | None:
        """The USDC moved from `payer` to `payee` in that transaction, or None when the
        transaction is unknown, failed, or moved nothing between them."""

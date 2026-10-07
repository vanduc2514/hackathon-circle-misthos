"""What the pricing engine may know about a publisher's money, and where it came from."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from misthos.domain.money import Usdc

# USDC settles one for one against dollars, so dollar amounts in a publisher's books
# are the same money. Anything else would need a rate we do not hold, so it is left
# out and the context says so.
DOLLARS = frozenset({"USD", "USDC"})


class FinanceError(Exception):
    """The books could not be read. The declared budget still applies."""


@dataclass(frozen=True)
class FinanceContext:
    source: str
    """Where the figures came from: "declared", "Firefly III" or "beancount"."""
    budget_remaining: Usdc | None
    """What is left of the budget this work comes out of, in the current period."""
    cash: Usdc | None
    """Cash on hand across the publisher's asset accounts, for the publisher's eyes."""
    as_of: datetime
    note: str = ""
    """What was left out or could not be read, in plain words."""


class FinanceSource(Protocol):
    """A publisher's books, read-only. Nothing here ever writes to them."""

    name: str

    def read(self, now: datetime) -> FinanceContext: ...


def dollars(value: object) -> Usdc:
    """A ledger amount as USDC, sign kept: books often record spending as negative."""
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise FinanceError(f"not an amount: {value!r}") from exc
    return Usdc(int((amount * 1_000_000).to_integral_value()))

"""Sanctions screening.

The real provider reads the OFAC SDN list. Tests inject their own provider at this
boundary.
"""

from __future__ import annotations

from typing import Protocol

from misthos.domain.compliance import ScreeningOutcome


class ScreeningProvider(Protocol):
    name: str

    def screen(self, wallet_address: str) -> tuple[ScreeningOutcome, str | None]:
        """The outcome for this wallet now, and the list it is on when it is a hit."""

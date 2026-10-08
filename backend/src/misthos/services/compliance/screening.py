"""Sanctions screening.

The simulated provider lists whatever it is told to, so a test or a demo can list a
contributor after they were verified and watch the next payout stop.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Protocol

from misthos.domain.compliance import ScreeningOutcome


class ScreeningProvider(Protocol):
    name: str

    def screen(self, wallet_address: str) -> tuple[ScreeningOutcome, str | None]:
        """The outcome for this wallet now, and the list it is on when it is a hit."""


class SimulatedScreening:
    name = "simulated"
    LIST_NAME = "SIMULATED-SDN"

    def __init__(self, listed: Iterable[str] = ()) -> None:
        self._guard = threading.Lock()
        self._listed = {a.strip().lower() for a in listed if a.strip()}

    def list_wallet(self, wallet_address: str) -> None:
        with self._guard:
            self._listed.add(wallet_address.lower())

    def delist_wallet(self, wallet_address: str) -> None:
        with self._guard:
            self._listed.discard(wallet_address.lower())

    def screen(self, wallet_address: str) -> tuple[ScreeningOutcome, str | None]:
        with self._guard:
            if wallet_address.lower() in self._listed:
                return ScreeningOutcome.HIT, self.LIST_NAME
        return ScreeningOutcome.CLEAR, None

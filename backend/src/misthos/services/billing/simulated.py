"""The simulation's rail: transfers it was asked to make, and nothing else."""

from __future__ import annotations

import secrets
import threading

from misthos.domain.money import Usdc
from misthos.services.billing.base import Received


class SimulatedRail:
    name = "simulated"

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._sent: dict[str, Received] = {}

    def send(self, payer: str, payee: str, amount: Usdc) -> str:
        """Move USDC from payer to payee, as the publisher's wallet would."""
        tx = "0x" + secrets.token_hex(32)
        with self._guard:
            self._sent[tx] = Received(tx_hash=tx, payer=payer, payee=payee, amount=amount)
        return tx

    def received(self, tx_hash: str, *, payer: str, payee: str) -> Received | None:
        with self._guard:
            found = self._sent.get(tx_hash.lower())
        if found is None:
            return None
        if found.payer.lower() != payer.lower() or found.payee.lower() != payee.lower():
            return None
        return found

    def reset(self) -> None:
        with self._guard:
            self._sent = {}

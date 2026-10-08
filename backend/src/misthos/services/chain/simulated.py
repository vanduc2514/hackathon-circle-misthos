"""A simulated escrow that keeps its own books.

It refuses what `MisthosEscrow` refuses (a second commitment, a settlement of
something not held, an amount that is not the whole commitment), so the store meets
the same failures in the simulation as on chain. Its books are separate from the
ledger on purpose: reconciliation compares two records, and a test can tamper with
this one to prove a divergence is caught.

With a database configured the books live in their own table, so a restart does not
make every issue look divergent; without one they are memory, like everything else.
Each call reads and writes its books as one step, so a reset that lands in the middle
of a release cannot have the release write the forgotten commitment back.
"""

from __future__ import annotations

import secrets
import threading
from datetime import datetime

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine

from misthos.domain.ledger import EscrowStatus, OnChain
from misthos.domain.money import Usdc
from misthos.models import tables as t
from misthos.services.chain.base import ChainRevert


def _tx() -> str:
    return "0x" + secrets.token_hex(32)


class SimulatedChain:
    name = "simulated"

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine
        # Re-entrant, because a call holds it across its own read and write.
        self._guard = threading.RLock()
        self._books: dict[str, tuple[str, Usdc, EscrowStatus]] = {}

    # ------------------------------------------------------------ the escrow

    def commit(
        self, issue_id: str, publisher: str, amount: Usdc, deadline: datetime, at: datetime
    ) -> str:
        if amount.base_units <= 0:
            raise ChainRevert("ZeroAmount")
        with self._guard:
            if self._get(issue_id) is not None:
                raise ChainRevert("AlreadyExists")
            tx = _tx()
            self._put(issue_id, publisher, amount, EscrowStatus.HELD, tx, insert_new=True)
            return tx

    def release(self, issue_id: str, contributor: str, amount: Usdc, at: datetime) -> str:
        return self._settle(issue_id, amount, EscrowStatus.RELEASED, at)

    def refund(self, issue_id: str, at: datetime) -> str:
        with self._guard:
            found = self._get(issue_id)
            if found is None:
                raise ChainRevert("NotHeld")
            return self._settle(issue_id, found[1], EscrowStatus.REFUNDED, at)

    def commitments(self) -> dict[str, OnChain]:
        if self._engine is None:
            with self._guard:
                return {k: OnChain(status=v[2], amount=v[1]) for k, v in self._books.items()}
        with self._engine.connect() as conn:
            rows = conn.execute(select(t.simulated_escrow)).mappings()
            return {
                r["issue_id"]: OnChain(
                    status=EscrowStatus(r["status"]), amount=Usdc(r["amount_base_units"])
                )
                for r in rows
            }

    def reset(self) -> None:
        with self._guard:
            if self._engine is None:
                self._books = {}
                return
            with self._engine.begin() as conn:
                conn.execute(delete(t.simulated_escrow))

    def tamper(self, issue_id: str, status: EscrowStatus) -> None:
        """Move a commitment without the platform, as a compromised key would. Tests only."""
        found = self._get(issue_id)
        assert found is not None, issue_id
        self._put(issue_id, found[0], found[1], status, _tx(), insert_new=False)

    # ---------------------------------------------------------------- books

    def _settle(self, issue_id: str, amount: Usdc, status: EscrowStatus, at: datetime) -> str:
        with self._guard:
            found = self._get(issue_id)
            if found is None or found[2] is not EscrowStatus.HELD:
                raise ChainRevert("NotHeld")
            if amount != found[1]:
                raise ChainRevert("AmountMismatch")
            tx = _tx()
            self._put(issue_id, found[0], found[1], status, tx, insert_new=False)
            return tx

    def _get(self, issue_id: str) -> tuple[str, Usdc, EscrowStatus] | None:
        if self._engine is None:
            with self._guard:
                return self._books.get(issue_id)
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    select(t.simulated_escrow).where(t.simulated_escrow.c.issue_id == issue_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return row["publisher_wallet"], Usdc(row["amount_base_units"]), EscrowStatus(row["status"])

    def _put(
        self,
        issue_id: str,
        party: str,
        amount: Usdc,
        status: EscrowStatus,
        tx: str,
        *,
        insert_new: bool,
    ) -> None:
        if self._engine is None:
            with self._guard:
                self._books[issue_id] = (party, amount, status)
            return
        values = {"status": status.value, "last_tx_hash": tx}
        with self._engine.begin() as conn:
            if insert_new:
                conn.execute(
                    insert(t.simulated_escrow).values(
                        issue_id=issue_id,
                        publisher_wallet=party,
                        amount_base_units=amount.base_units,
                        **values,
                    )
                )
            else:
                conn.execute(
                    update(t.simulated_escrow)
                    .where(t.simulated_escrow.c.issue_id == issue_id)
                    .values(**values)
                )

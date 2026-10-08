"""A simulated escrow that keeps its own books.

It refuses what `MisthosEscrow` refuses (a commitment with no ceiling or above it,
a second commitment, a settlement of something not held, an amount that is not the
whole commitment), so the store meets
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
        self._books: dict[str, tuple[str, Usdc, EscrowStatus, int]] = {}
        self._ceilings: dict[str, Usdc] = {}

    # ------------------------------------------------------------ the escrow

    def set_ceiling(self, issue_id: str, ceiling: Usdc, at: datetime) -> str:
        with self._guard:
            self._put_ceiling(issue_id, ceiling)
            return _tx()

    def escrow_ceiling(self, issue_id: str) -> Usdc | None:
        if self._engine is None:
            with self._guard:
                return self._ceilings.get(issue_id)
        with self._engine.connect() as conn:
            value = conn.execute(
                select(t.simulated_escrow_ceilings.c.ceiling_base_units).where(
                    t.simulated_escrow_ceilings.c.issue_id == issue_id
                )
            ).scalar()
        return Usdc(value) if value else None

    def commit(
        self,
        issue_id: str,
        publisher: str,
        amount: Usdc,
        deadline: datetime,
        at: datetime,
        fee_bps: int = 0,
    ) -> str:
        if fee_bps < 0:
            raise ChainRevert("FeeTooHigh")
        with self._guard:
            # The contract's order: an existing commitment, then the amount, then
            # the ceiling, so the same call fails the same way in both places.
            if self._get(issue_id) is not None:
                raise ChainRevert("AlreadyExists")
            if amount.base_units <= 0:
                raise ChainRevert("ZeroAmount")
            cap = self.escrow_ceiling(issue_id)
            if cap is None:
                raise ChainRevert("NoCeiling")
            if amount.base_units > cap.base_units:
                raise ChainRevert(f"ExceedsCeiling({amount.base_units}, {cap.base_units})")
            tx = _tx()
            self._put(issue_id, publisher, amount, EscrowStatus.HELD, tx, fee_bps, insert_new=True)
            return tx

    def release(self, issue_id: str, contributor: str, amount: Usdc, at: datetime) -> str:
        return self._settle(issue_id, amount, EscrowStatus.RELEASED, at)

    def refund(self, issue_id: str, at: datetime) -> str:
        with self._guard:
            found = self._get(issue_id)
            if found is None:
                raise ChainRevert("NotHeld")
            return self._settle(issue_id, found[1], EscrowStatus.REFUNDED, at)

    def commitment(self, issue_id: str) -> OnChain | None:
        found = self._get(issue_id)
        if found is None:
            return None
        return OnChain(status=found[2], amount=found[1], fee_bps=found[3], publisher=found[0])

    def commitments(self) -> dict[str, OnChain]:
        if self._engine is None:
            with self._guard:
                return {
                    k: OnChain(status=v[2], amount=v[1], fee_bps=v[3], publisher=v[0])
                    for k, v in self._books.items()
                }
        with self._engine.connect() as conn:
            rows = conn.execute(select(t.simulated_escrow)).mappings()
            return {
                r["issue_id"]: OnChain(
                    status=EscrowStatus(r["status"]),
                    amount=Usdc(r["amount_base_units"]),
                    fee_bps=r["fee_bps"],
                    publisher=r["publisher_wallet"],
                )
                for r in rows
            }

    def reset(self) -> None:
        with self._guard:
            if self._engine is None:
                self._books = {}
                self._ceilings = {}
                return
            with self._engine.begin() as conn:
                conn.execute(delete(t.simulated_escrow))
                conn.execute(delete(t.simulated_escrow_ceilings))

    def tamper(self, issue_id: str, status: EscrowStatus) -> None:
        """Move a commitment without the platform, as a compromised key would. Tests only."""
        found = self._get(issue_id)
        assert found is not None, issue_id
        self._put(issue_id, found[0], found[1], status, _tx(), found[3], insert_new=False)

    # ---------------------------------------------------------------- books

    def _settle(self, issue_id: str, amount: Usdc, status: EscrowStatus, at: datetime) -> str:
        with self._guard:
            found = self._get(issue_id)
            if found is None or found[2] is not EscrowStatus.HELD:
                raise ChainRevert("NotHeld")
            if amount != found[1]:
                raise ChainRevert("AmountMismatch")
            tx = _tx()
            self._put(issue_id, found[0], found[1], status, tx, found[3], insert_new=False)
            return tx

    def _put_ceiling(self, issue_id: str, ceiling: Usdc) -> None:
        # Zero clears the ceiling, as `setCeiling(id, 0)` does: unfundable, not uncapped.
        if self._engine is None:
            if ceiling.base_units:
                self._ceilings[issue_id] = ceiling
            else:
                self._ceilings.pop(issue_id, None)
            return
        table = t.simulated_escrow_ceilings
        with self._engine.begin() as conn:
            conn.execute(delete(table).where(table.c.issue_id == issue_id))
            if ceiling.base_units:
                conn.execute(
                    insert(table).values(issue_id=issue_id, ceiling_base_units=ceiling.base_units)
                )

    def _get(self, issue_id: str) -> tuple[str, Usdc, EscrowStatus, int] | None:
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
        return (
            row["publisher_wallet"],
            Usdc(row["amount_base_units"]),
            EscrowStatus(row["status"]),
            row["fee_bps"],
        )

    def _put(
        self,
        issue_id: str,
        party: str,
        amount: Usdc,
        status: EscrowStatus,
        tx: str,
        fee_bps: int,
        *,
        insert_new: bool,
    ) -> None:
        if self._engine is None:
            with self._guard:
                self._books[issue_id] = (party, amount, status, fee_bps)
            return
        values = {"status": status.value, "last_tx_hash": tx}
        with self._engine.begin() as conn:
            if insert_new:
                conn.execute(
                    insert(t.simulated_escrow).values(
                        issue_id=issue_id,
                        publisher_wallet=party,
                        amount_base_units=amount.base_units,
                        fee_bps=fee_bps,
                        **values,
                    )
                )
            else:
                conn.execute(
                    update(t.simulated_escrow)
                    .where(t.simulated_escrow.c.issue_id == issue_id)
                    .values(**values)
                )

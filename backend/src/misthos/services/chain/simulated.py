"""A simulated escrow that keeps its own books.

It refuses what `MisthosEscrow` refuses (a commitment with no ceiling or above it, from
a wallet the approval did not name or to a deadline past the approved one, a second
commitment, a settlement of something not held, an amount that is not the whole
commitment, a release after the deadline and a refund before it), so the store meets
the same failures in the simulation as on chain. The deadlines matter most: a
simulation that paid after the deadline is how the release that #121 found could not
land went unnoticed. The block's clock is the `at` each call is given.

Its books are separate from the ledger on purpose: reconciliation compares two
records, and a test can tamper with this one to prove a divergence is caught.

With a database configured the books live in their own table, so a restart does not
make every issue look divergent; without one they are memory, like everything else.
Each call reads and writes its books as one step, so a reset that lands in the middle
of a release cannot have the release write the forgotten commitment back.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine

from misthos.domain.ledger import EscrowStatus, OnChain
from misthos.domain.money import Usdc
from misthos.models import tables as t
from misthos.services.chain.base import ChainRevert


def _tx() -> str:
    return "0x" + secrets.token_hex(32)


def _seconds(moment: datetime) -> int:
    """A moment as the contract holds it: whole Unix seconds."""
    return int(moment.timestamp())


def _utc(moment: datetime | None) -> datetime | None:
    # SQLite hands timestamps back without a zone; everything here is stored in UTC.
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


@dataclass(frozen=True)
class _Held:
    publisher: str
    amount: Usdc
    status: EscrowStatus
    fee_bps: int
    deadline: datetime | None
    commit_tx: str

    def on_chain(self) -> OnChain:
        return OnChain(
            status=self.status,
            amount=self.amount,
            fee_bps=self.fee_bps,
            publisher=self.publisher,
            deadline=self.deadline,
        )


@dataclass(frozen=True)
class _Approval:
    ceiling: Usdc
    publisher: str | None
    latest: datetime | None


class SimulatedChain:
    name = "simulated"

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine
        # Re-entrant, because a call holds it across its own read and write.
        self._guard = threading.RLock()
        self._books: dict[str, _Held] = {}
        self._approvals: dict[str, _Approval] = {}

    # ------------------------------------------------------------ the escrow

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
        # The simulated commitment carries its own rate (the platform sends it), so the
        # rate is not kept here; the approval's other two terms are, as on chain.
        if ceiling.base_units:
            if not publisher:
                raise ChainRevert("ZeroAddress")
            if _seconds(latest_deadline) <= _seconds(at):
                raise ChainRevert("DeadlinePassed")
        with self._guard:
            self._put_approval(issue_id, _Approval(ceiling, publisher, latest_deadline))
            return _tx()

    def escrow_ceiling(self, issue_id: str) -> Usdc | None:
        approval = self._approval(issue_id)
        return approval.ceiling if approval is not None else None

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
            # The contract's order, so the same call fails the same way in both places.
            if self._get(issue_id) is not None:
                raise ChainRevert("AlreadyExists")
            if amount.base_units <= 0:
                raise ChainRevert("ZeroAmount")
            if _seconds(deadline) <= _seconds(at):
                raise ChainRevert("DeadlinePassed")
            approval = self._approval(issue_id)
            if approval is None:
                raise ChainRevert("NoCeiling")
            if (approval.publisher or "").lower() != publisher.lower():
                raise ChainRevert(f"NotApprovedPublisher({publisher})")
            if amount.base_units > approval.ceiling.base_units:
                raise ChainRevert(
                    f"ExceedsCeiling({amount.base_units}, {approval.ceiling.base_units})"
                )
            if approval.latest is not None and _seconds(deadline) > _seconds(approval.latest):
                raise ChainRevert(
                    f"DeadlineTooLate({_seconds(deadline)}, {_seconds(approval.latest)})"
                )
            tx = _tx()
            held = _Held(publisher, amount, EscrowStatus.HELD, fee_bps, deadline, tx)
            self._put(issue_id, held, tx, insert_new=True)
            return tx

    def commit_tx(self, issue_id: str) -> str:
        found = self._get(issue_id)
        if found is None:
            raise ChainRevert("NotHeld")
        return found.commit_tx

    def release(self, issue_id: str, contributor: str, amount: Usdc, at: datetime) -> str:
        with self._guard:
            found = self._held(issue_id)
            if found.deadline is not None and _seconds(at) > _seconds(found.deadline):
                raise ChainRevert("DeadlinePassed")
            return self._settle(issue_id, found, amount, EscrowStatus.RELEASED)

    def refund(self, issue_id: str, at: datetime) -> str:
        with self._guard:
            found = self._held(issue_id)
            if found.deadline is not None and _seconds(at) < _seconds(found.deadline):
                raise ChainRevert("DeadlineNotReached")
            return self._settle(issue_id, found, found.amount, EscrowStatus.REFUNDED)

    def commitment(self, issue_id: str) -> OnChain | None:
        found = self._get(issue_id)
        return found.on_chain() if found is not None else None

    def commitments(self) -> dict[str, OnChain]:
        if self._engine is None:
            with self._guard:
                return {k: v.on_chain() for k, v in self._books.items()}
        with self._engine.connect() as conn:
            rows = conn.execute(select(t.simulated_escrow)).mappings()
            return {r["issue_id"]: _held_from(r).on_chain() for r in rows}

    def settlement_problems(self) -> list[str]:
        return []

    def reset(self) -> None:
        with self._guard:
            if self._engine is None:
                self._books = {}
                self._approvals = {}
                return
            with self._engine.begin() as conn:
                conn.execute(delete(t.simulated_escrow))
                conn.execute(delete(t.simulated_escrow_ceilings))

    def tamper(self, issue_id: str, status: EscrowStatus) -> None:
        """Move a commitment without the platform, as a compromised key would. Tests only."""
        found = self._get(issue_id)
        assert found is not None, issue_id
        self._put(issue_id, replace(found, status=status), _tx(), insert_new=False)

    # ---------------------------------------------------------------- books

    def _held(self, issue_id: str) -> _Held:
        found = self._get(issue_id)
        if found is None or found.status is not EscrowStatus.HELD:
            raise ChainRevert("NotHeld")
        return found

    def _settle(self, issue_id: str, found: _Held, amount: Usdc, status: EscrowStatus) -> str:
        if amount != found.amount:
            raise ChainRevert("AmountMismatch")
        tx = _tx()
        self._put(issue_id, replace(found, status=status), tx, insert_new=False)
        return tx

    def _approval(self, issue_id: str) -> _Approval | None:
        if self._engine is None:
            with self._guard:
                return self._approvals.get(issue_id)
        table = t.simulated_escrow_ceilings
        with self._engine.connect() as conn:
            row = conn.execute(select(table).where(table.c.issue_id == issue_id)).mappings().first()
        if row is None or not row["ceiling_base_units"]:
            return None
        return _Approval(
            Usdc(row["ceiling_base_units"]), row["publisher_wallet"], _utc(row["latest_deadline"])
        )

    def _put_approval(self, issue_id: str, approval: _Approval) -> None:
        # Zero clears the approval, as `setCeiling(id, 0, ...)` does: unfundable, not
        # uncapped, and nobody named to commit.
        if self._engine is None:
            if approval.ceiling.base_units:
                self._approvals[issue_id] = approval
            else:
                self._approvals.pop(issue_id, None)
            return
        table = t.simulated_escrow_ceilings
        with self._engine.begin() as conn:
            conn.execute(delete(table).where(table.c.issue_id == issue_id))
            if approval.ceiling.base_units:
                conn.execute(
                    insert(table).values(
                        issue_id=issue_id,
                        ceiling_base_units=approval.ceiling.base_units,
                        publisher_wallet=approval.publisher,
                        latest_deadline=approval.latest,
                    )
                )

    def _get(self, issue_id: str) -> _Held | None:
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
        return _held_from(row) if row is not None else None

    def _put(self, issue_id: str, held: _Held, tx: str, *, insert_new: bool) -> None:
        if self._engine is None:
            with self._guard:
                self._books[issue_id] = held
            return
        values = {"status": held.status.value, "last_tx_hash": tx}
        with self._engine.begin() as conn:
            if insert_new:
                conn.execute(
                    insert(t.simulated_escrow).values(
                        issue_id=issue_id,
                        publisher_wallet=held.publisher,
                        amount_base_units=held.amount.base_units,
                        fee_bps=held.fee_bps,
                        deadline=held.deadline,
                        commit_tx_hash=held.commit_tx,
                        **values,
                    )
                )
            else:
                conn.execute(
                    update(t.simulated_escrow)
                    .where(t.simulated_escrow.c.issue_id == issue_id)
                    .values(**values)
                )


def _held_from(row) -> _Held:  # type: ignore[no-untyped-def]
    return _Held(
        publisher=row["publisher_wallet"],
        amount=Usdc(row["amount_base_units"]),
        status=EscrowStatus(row["status"]),
        fee_bps=row["fee_bps"],
        deadline=_utc(row["deadline"]),
        commit_tx=row["commit_tx_hash"] or row["last_tx_hash"],
    )

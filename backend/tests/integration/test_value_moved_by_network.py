"""Value moved, kept apart by network and by what the money was (#31).

The judges weight real USDC above test USDC and count none for a simulation, so a
dashboard that adds them into one figure claims more than happened. The simulation
and Arc testnet share a chain id, so the chain alone could not tell them apart: each
commitment now records what its money was when it was committed.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from misthos.domain.ledger import MoneyEventKind
from misthos.repositories.sql import SqlRepository
from misthos.services import metrics
from misthos.store import Store, store


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield


def released(records):  # type: ignore[no-untyped-def]
    return [(r, e) for r in records for e in r.money_events if e.kind is MoneyEventKind.RELEASED]


class TestTheSimulation:
    def test_its_settlements_are_never_reported_as_test_money(self) -> None:
        store.approve_and_accept("ISS-1006")
        m = store.metrics()
        assert m.value_moved, "the demo has settled work"
        assert {(row.chain, row.money) for row in m.value_moved} == {("arc-testnet", "simulated")}
        (row,) = m.value_moved
        assert row.settled_usdc == m.matched_volume_usdc
        assert row.platform_fees_usdc == m.platform_fees_usdc
        assert row.settled_issues == m.settled_issues

    def test_the_public_loop_keeps_them_apart_too(self) -> None:
        loop = store.loop()
        assert [row.money for row in loop.value_moved] == ["simulated"]
        assert loop.value_moved[0].settled_usdc == loop.matched_volume_usdc

    def test_what_the_money_was_survives_a_restart(self, tmp_path: Path) -> None:
        url = f"sqlite:///{tmp_path / 'm.db'}"
        first = Store(SqlRepository(url))
        first.reset()
        first.approve_and_accept("ISS-1006")
        restarted = Store(SqlRepository(url))
        escrow = restarted.get("ISS-1006").escrow  # type: ignore[union-attr]
        assert escrow is not None and escrow.money == "simulated"


class TestKeepingThemApart:
    def test_each_kind_of_money_is_its_own_row_and_real_money_comes_first(self) -> None:
        store.approve_and_accept("ISS-1006")
        paid = [r for r, _ in released(store.list_issues())]
        assert len(paid) >= 3
        records = [copy.deepcopy(r) for r in paid[:3]]
        for rec, (chain, kind) in zip(
            records,
            [("arc-testnet", "test"), ("arc-mainnet", "real"), ("arc-testnet", None)],
            strict=True,
        ):
            assert rec.escrow is not None
            rec.escrow = rec.escrow.model_copy(update={"chain": chain, "money": kind})

        rows = metrics.value_moved(released(records))
        assert [(row.chain, row.money) for row in rows] == [
            ("arc-mainnet", "real"),
            ("arc-testnet", "test"),
            ("arc-testnet", "unrecorded"),
        ]
        assert all(row.settled_issues == 1 for row in rows)
        by_kind = {row.money: row.settled_usdc for row in rows}
        for rec, kind in zip(records, ["test", "real", "unrecorded"], strict=True):
            (event,) = [e for e in rec.money_events if e.kind is MoneyEventKind.RELEASED]
            assert by_kind[kind] == f"{event.amount.decimal:.2f}"

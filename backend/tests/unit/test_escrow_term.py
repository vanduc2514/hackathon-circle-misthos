"""A publisher names the escrow term, inside bounds, and it is fixed with the money.

The 14-day constant was the only clock an issue could run on. Now the publisher
names a whole number of days at publish time (#172), refused outside `MIN_ESCROW_TERM`
to `MAX_ESCROW_TERM` with the bound named, defaulted to 14 when unnamed, derived into
the funding deadline, and frozen once the escrow holds a commitment.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from misthos.domain.issue import (
    ESCROW_TERM,
    MAX_ESCROW_TERM,
    MIN_ESCROW_TERM,
    EscrowTermRefused,
    check_escrow_term,
)
from misthos.repositories import MemoryRepository
from misthos.schemas import PublishRequest
from misthos.store import Store

REPO = "acme/ledger"
PUBLISHER = "PUB-1"


def make_store() -> Store:
    store = Store(MemoryRepository())
    store.reset()
    return store


def publish_with(store: Store, **kwargs) -> str:
    return store.publish(
        PublishRequest(
            repo=REPO,
            title="Retry on 503",
            summary="The retry budget ignores jitter.",
            labels=["bug"],
            publisher_id=PUBLISHER,
            **kwargs,
        )
    ).id


# --- The validator, in isolation -------------------------------------------


def test_floor_refuses_a_term_below_the_minimum() -> None:
    with pytest.raises(EscrowTermRefused) as exc:
        check_escrow_term(MIN_ESCROW_TERM.days - 1)
    assert str(MIN_ESCROW_TERM.days) in exc.value.justification


def test_ceiling_refuses_a_term_above_the_maximum() -> None:
    with pytest.raises(EscrowTermRefused) as exc:
        check_escrow_term(MAX_ESCROW_TERM.days + 1)
    assert str(MAX_ESCROW_TERM.days) in exc.value.justification


def test_bounds_themselves_are_accepted() -> None:
    assert check_escrow_term(MIN_ESCROW_TERM.days) == MIN_ESCROW_TERM
    assert check_escrow_term(MAX_ESCROW_TERM.days) == MAX_ESCROW_TERM


def test_non_whole_number_of_days_is_refused() -> None:
    with pytest.raises(EscrowTermRefused) as exc:
        check_escrow_term(3.5)
    assert "whole number" in exc.value.justification


def test_zero_and_negative_terms_are_refused() -> None:
    for bad in (0, -1, -14):
        with pytest.raises(EscrowTermRefused):
            check_escrow_term(bad)


def test_a_non_number_is_refused() -> None:
    for bad in ("14", None, True):
        with pytest.raises(EscrowTermRefused):
            check_escrow_term(bad)  # type: ignore[arg-type]


# --- The publish path --------------------------------------------------------


def test_publishing_without_a_term_stays_default() -> None:
    store = make_store()
    rec = store.get(publish_with(store))
    assert rec is not None
    assert rec.escrow_term is None


def test_publishing_a_term_stores_it_on_the_issue() -> None:
    store = make_store()
    rec = store.get(publish_with(store, escrow_term_days=30))
    assert rec is not None
    assert rec.escrow_term == timedelta(days=30)


def test_publishing_a_term_outside_the_bounds_names_the_bound() -> None:
    store = make_store()
    with pytest.raises(EscrowTermRefused) as below:
        publish_with(store, escrow_term_days=2)
    assert str(MIN_ESCROW_TERM.days) in below.value.justification
    with pytest.raises(EscrowTermRefused) as above:
        publish_with(store, escrow_term_days=91)
    assert str(MAX_ESCROW_TERM.days) in above.value.justification
    # A fractional term never reaches the validator: the request schema refuses it.
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        publish_with(store, escrow_term_days=7.5)


# --- The deadline derives from the term -------------------------------------


def _fund_via_lifecycle(store: Store, issue_id: str):
    """Move a freshly published issue to FUNDED through the store's real path."""
    from misthos.domain.money import Usdc

    rec = store.get(issue_id)
    assert rec is not None
    rec.criteria_approved_at = rec.created_at + timedelta(hours=5)
    store.repo.save_issues(rec)
    store.set_price(issue_id, Usdc.from_decimal("50"), by=PUBLISHER)
    rec = store.get(issue_id)
    assert rec is not None
    store._fund(rec, when=rec.created_at + timedelta(hours=6))
    store.repo.save_issues(rec)
    return store.get(issue_id)


def test_funding_derives_the_deadline_from_the_named_term() -> None:
    store = make_store()
    issue_id = publish_with(store, escrow_term_days=30)
    store_funded = _fund_via_lifecycle(store, issue_id)
    assert store_funded is not None and store_funded.funding is not None
    expected = (
        store_funded.created_at + timedelta(hours=6) + timedelta(days=30)
    ).replace(microsecond=0)
    assert store_funded.funding.deadline == expected


def test_funding_without_a_term_keeps_the_default_deadline() -> None:
    store = make_store()
    issue_id = publish_with(store)
    rec = _fund_via_lifecycle(store, issue_id)
    assert rec is not None and rec.funding is not None
    expected = (rec.created_at + timedelta(hours=6) + ESCROW_TERM).replace(microsecond=0)
    assert rec.funding.deadline == expected

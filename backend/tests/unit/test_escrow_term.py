"""Unit and integration tests for configurable escrow term (#172).

Acceptance criteria:
- Publisher can name an escrow term in whole days at publish time.
- Default is 14 days when publisher does not name one.
- Bounds: MIN_ESCROW_TERM = 3 days, MAX_ESCROW_TERM = 90 days.
- Zero, negative, fractional, or non-number is refused with why.
- Deadline is derived as approved_at + term.
- Term is fixed once money is committed to escrow.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from misthos.domain.issue import (
    DEFAULT_ESCROW_TERM,
    MAX_ESCROW_TERM,
    MIN_ESCROW_TERM,
    EscrowTermRefused,
    IssueState,
    check_escrow_term,
)
from misthos.repositories import MemoryRepository
from misthos.schemas import PublishRequest
from misthos.services.github import SimulatedGitHub
from misthos.store import Store

REPO = "acme/ledger"
PUBLISHER = "PUB-1"


def make_store() -> Store:
    store = Store(MemoryRepository(), github=SimulatedGitHub())
    store.reset()
    return store


class TestEscrowTermValidation:
    """Domain validation of the escrow term bounds (#172)."""

    def test_default_is_fourteen_days(self) -> None:
        assert check_escrow_term(None) == timedelta(days=14)
        assert check_escrow_term(None) == DEFAULT_ESCROW_TERM

    def test_floor_bound_names_minimum(self) -> None:
        with pytest.raises(EscrowTermRefused, match="minimum escrow term is 3 days"):
            check_escrow_term(2)
        with pytest.raises(EscrowTermRefused, match="minimum escrow term is 3 days"):
            check_escrow_term(timedelta(days=1))

    def test_ceiling_bound_names_maximum(self) -> None:
        with pytest.raises(EscrowTermRefused, match="maximum escrow term is 90 days"):
            check_escrow_term(91)
        with pytest.raises(EscrowTermRefused, match="maximum escrow term is 90 days"):
            check_escrow_term(timedelta(days=95))

    def test_valid_terms_inside_bounds(self) -> None:
        assert check_escrow_term(3) == timedelta(days=3)
        assert check_escrow_term(14) == timedelta(days=14)
        assert check_escrow_term(90) == timedelta(days=90)
        assert check_escrow_term("7") == timedelta(days=7)
        assert check_escrow_term(timedelta(days=30)) == timedelta(days=30)

    def test_non_whole_number_or_fractional_term_is_refused(self) -> None:
        with pytest.raises(EscrowTermRefused, match="fractional"):
            check_escrow_term(3.5)
        with pytest.raises(EscrowTermRefused, match="not a whole number of days"):
            check_escrow_term(timedelta(hours=12))
        with pytest.raises(EscrowTermRefused, match="not a number"):
            check_escrow_term("not-a-number")

    def test_zero_or_negative_is_refused(self) -> None:
        with pytest.raises(EscrowTermRefused, match="zero or negative"):
            check_escrow_term(0)
        with pytest.raises(EscrowTermRefused, match="zero or negative"):
            check_escrow_term(-7)


class TestEscrowTermInStore:
    """Store publish flow and term lifecycle (#172)."""

    def test_publish_with_default_term(self) -> None:
        store = make_store()
        rec = store.publish(
            PublishRequest(
                repo=REPO,
                title="Default Term Issue",
                summary="Summary",
                labels=["enhancement"],
                publisher_id=PUBLISHER,
                price="100",
            )
        )
        assert rec.escrow_term == timedelta(days=14)
        out = store.to_out(rec)
        assert out.escrow_term == 14
        assert out.escrow_term_days == 14

    def test_publish_with_custom_term(self) -> None:
        store = make_store()
        rec = store.publish(
            PublishRequest(
                repo=REPO,
                title="Custom Term Issue",
                summary="Summary",
                labels=["enhancement"],
                publisher_id=PUBLISHER,
                price="100",
                escrow_term=30,
            )
        )
        assert rec.escrow_term == timedelta(days=30)
        out = store.to_out(rec)
        assert out.escrow_term == 30
        assert out.escrow_term_days == 30

    def test_publish_with_invalid_term_is_refused(self) -> None:
        store = make_store()
        with pytest.raises(
            EscrowTermRefused, match=f"minimum escrow term is {MIN_ESCROW_TERM.days} days"
        ):
            store.publish(
                PublishRequest(
                    repo=REPO,
                    title="Too Short",
                    summary="Summary",
                    labels=["bug"],
                    publisher_id=PUBLISHER,
                    price="100",
                    escrow_term=2,
                )
            )

        with pytest.raises(
            EscrowTermRefused, match=f"maximum escrow term is {MAX_ESCROW_TERM.days} days"
        ):
            store.publish(
                PublishRequest(
                    repo=REPO,
                    title="Too Long",
                    summary="Summary",
                    labels=["bug"],
                    publisher_id=PUBLISHER,
                    price="100",
                    escrow_term=120,
                )
            )

    def test_funding_terms_derive_deadline_from_escrow_term(self) -> None:
        store = make_store()
        rec = store.publish(
            PublishRequest(
                repo=REPO,
                title="Term Deadline",
                summary="Summary",
                labels=["enhancement"],
                publisher_id=PUBLISHER,
                price="100",
                escrow_term=7,
            )
        )
        now = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
        pub = store.repo.get_publisher(rec.publisher_id)
        assert pub is not None
        terms = store._funding_terms(rec, pub, at=now)
        assert terms.deadline == datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC)

    def test_set_escrow_term_before_funding(self) -> None:
        store = make_store()
        rec = store.publish(
            PublishRequest(
                repo=REPO,
                title="Change Term",
                summary="Summary",
                labels=["enhancement"],
                publisher_id=PUBLISHER,
                price="100",
            )
        )
        assert rec.escrow_term == timedelta(days=14)
        updated = store.set_escrow_term(rec.id, 21, by="publisher")
        assert updated.escrow_term == timedelta(days=21)

    def test_escrow_term_cannot_move_after_funding(self) -> None:
        store = make_store()
        rec = store.publish(
            PublishRequest(
                repo=REPO,
                title="Funded Issue",
                summary="Summary",
                labels=["enhancement"],
                publisher_id=PUBLISHER,
                price="100",
                escrow_term=14,
            )
        )
        # Simulate funding: move state out of pre-funding
        rec.state = IssueState.FUNDED
        store.repo.save_issues(rec)

        with pytest.raises(EscrowTermRefused, match="the money is already committed to escrow"):
            store.set_escrow_term(rec.id, 30, by="publisher")

"""An organisation's spending policy, as arithmetic."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from misthos.domain.ledger import MoneyEvent, MoneyEventKind
from misthos.domain.money import Usdc
from misthos.domain.policy import (
    PolicyRefusal,
    SpendingPolicy,
    approves_own_payout,
    category_breaches,
    committed_this_month,
    login,
    may_approve,
    month_start,
    needs_approval,
)

USD = Usdc.from_decimal


def test_releases_above_the_threshold_need_an_approver() -> None:
    policy = SpendingPolicy(approval_threshold=USD("500"), approvers=("dana",))
    assert needs_approval(policy, USD("500.01"))
    assert not needs_approval(policy, USD("500"))
    assert not needs_approval(SpendingPolicy(), USD("1000000"))


def test_only_a_named_approver_approves() -> None:
    policy = SpendingPolicy(approval_threshold=USD("1"), approvers=("@Dana-Acme",))
    assert may_approve(policy, " dana-acme ")
    assert may_approve(policy, "@DANA-ACME")
    assert not may_approve(policy, "mallory")


def test_a_threshold_nobody_can_approve_is_refused() -> None:
    with pytest.raises(PolicyRefusal, match="named approver"):
        SpendingPolicy(approval_threshold=USD("1")).validate()


@pytest.mark.parametrize("named", ["dana@acme.example", "Dana Smith", "-dana", "x" * 40])
def test_an_approver_no_session_could_prove_is_refused(named: str) -> None:
    """A session proves a GitHub login, so a name that cannot be one would hold every
    payout over the threshold for good."""
    with pytest.raises(PolicyRefusal, match="GitHub login"):
        SpendingPolicy(approval_threshold=USD("1"), approvers=(named,)).validate()


def test_an_approver_is_named_the_way_github_prints_a_login() -> None:
    assert login("  @dana-acme ") == "dana-acme"
    SpendingPolicy(approval_threshold=USD("1"), approvers=("@dana-acme", "erin42")).validate()


def test_the_contributor_paid_is_not_their_own_approver() -> None:
    assert approves_own_payout("@Hal-Dev", "hal-dev")
    assert not approves_own_payout("dana-acme", "hal-dev")


def committed(amount: str, at: datetime) -> MoneyEvent:
    return MoneyEvent("ISS-1", MoneyEventKind.COMMITTED, USD(amount), "PUB-1", "0x1", at)


def test_the_month_is_the_calendar_month_the_limit_is_checked_in() -> None:
    now = datetime(2026, 10, 8, 12, tzinfo=UTC)
    assert month_start(now) == datetime(2026, 10, 1, tzinfo=UTC)
    issues = [
        (["security"], [committed("100", datetime(2026, 10, 1, tzinfo=UTC))]),
        (["security", "docs"], [committed("50", datetime(2026, 10, 7, tzinfo=UTC))]),
        # The last moment of September is last month, though it is this year.
        (["security"], [committed("900", datetime(2026, 9, 30, 23, 59, tzinfo=UTC))]),
    ]
    assert committed_this_month(issues, now) == {"security": USD("150"), "docs": USD("50")}


def test_only_commitments_count_against_a_monthly_limit() -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    at = datetime(2026, 10, 2, tzinfo=UTC)
    released = MoneyEvent("ISS-1", MoneyEventKind.RELEASED, USD("100"), "CON-1", "0x2", at)
    assert committed_this_month([(["security"], [committed("100", at), released])], now) == {
        "security": USD("100")
    }


def test_a_category_limit_counts_the_months_commitments() -> None:
    policy = SpendingPolicy(category_limits={"security": USD("1000")})
    assert category_breaches(policy, ["security"], USD("400"), {"security": USD("600")}) == []
    [breach] = category_breaches(policy, ["security", "docs"], USD("401"), {"security": USD("600")})
    assert "security is limited to 1000.00 USDC a month" in breach
    assert "600.00 USDC is committed already" in breach


def test_unlimited_categories_are_not_limited() -> None:
    policy = SpendingPolicy(category_limits={"security": USD("10")})
    assert category_breaches(policy, ["feature"], USD("5000"), {}) == []

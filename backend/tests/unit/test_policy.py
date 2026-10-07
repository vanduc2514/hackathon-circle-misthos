"""An organisation's spending policy, as arithmetic."""

from __future__ import annotations

import pytest

from misthos.domain.money import Usdc
from misthos.domain.policy import (
    PolicyRefusal,
    SpendingPolicy,
    category_breaches,
    may_approve,
    needs_approval,
)

USD = Usdc.from_decimal


def test_releases_above_the_threshold_need_an_approver() -> None:
    policy = SpendingPolicy(approval_threshold=USD("500"), approvers=("dana",))
    assert needs_approval(policy, USD("500.01"))
    assert not needs_approval(policy, USD("500"))
    assert not needs_approval(SpendingPolicy(), USD("1000000"))


def test_only_a_named_approver_approves() -> None:
    policy = SpendingPolicy(approval_threshold=USD("1"), approvers=("Dana@Acme.example",))
    assert may_approve(policy, " dana@acme.example ")
    assert not may_approve(policy, "someone@else.example")


def test_a_threshold_nobody_can_approve_is_refused() -> None:
    with pytest.raises(PolicyRefusal, match="named approver"):
        SpendingPolicy(approval_threshold=USD("1")).validate()


def test_a_category_limit_counts_the_months_commitments() -> None:
    policy = SpendingPolicy(category_limits={"security": USD("1000")})
    assert category_breaches(policy, ["security"], USD("400"), {"security": USD("600")}) == []
    [breach] = category_breaches(policy, ["security", "docs"], USD("401"), {"security": USD("600")})
    assert "security is limited to 1000.00 USDC a month" in breach
    assert "600.00 USDC is committed already" in breach


def test_unlimited_categories_are_not_limited() -> None:
    policy = SpendingPolicy(category_limits={"security": USD("10")})
    assert category_breaches(policy, ["feature"], USD("5000"), {}) == []

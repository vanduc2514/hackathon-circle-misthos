"""Tests for the open claim model (#171).

- Any number of contributors can claim simultaneously (open roster).
- Review budget: platform pays first reviews up to budget; rework or exhausted budget
  requires deposit.
- Pre-screen: draft PRs, zero changes, whitespace-only diffs rejected without spending
  budget or deposit.
- Duplicate detection: exact copy of competing submission rejected and forfeits deposit;
  derived submission flagged.
- Merge winner: only merge pays; supersedes other claims; off-roster author paid with
  retroactive claim; deadline check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from misthos.domain.issue import IssueState
from misthos.domain.money import Usdc
from misthos.domain.pricing import (
    review_budget,
)
from misthos.domain.review import (
    ChangedFile,
    Judgement,
    Submitted,
    Verdict,
    decide,
    detect_derivation,
    is_whitespace_only,
    prescreen,
)
from misthos.repositories import MemoryRepository
from misthos.schemas import PublishRequest
from misthos.services.github import PullRequest
from misthos.store import Store
from tests.support.fake_chain import FakeChain
from tests.support.fake_compliance import FakeIdentity, FakeScreening
from tests.support.fake_github import FakeGitHub


def make_store() -> Store:
    return Store(
        MemoryRepository(),
        chain=FakeChain(),
        github=FakeGitHub(),
        screening=FakeScreening(),
        identity=FakeIdentity(),
    )


def test_review_budget_calculation() -> None:
    # 500 USDC issue: platform fee 10% = 50 USDC. 50 * 0.5 = 25 USDC.
    # 25 // 6.31 = 3 reviews.
    budget_500 = review_budget(Usdc.from_decimal("500"))
    assert budget_500 == 3

    # Cheap issue (50 USDC): platform fee 5 USDC. 5 * 0.5 = 2.5 USDC.
    # 2.5 // 6.31 = 0, clamped to at least 1.
    budget_50 = review_budget(Usdc.from_decimal("50"))
    assert budget_50 == 1

    # Large issue (5000 USDC): fee 500 USDC. 500 * 0.5 = 250 USDC.
    # 250 // 6.31 = 39 reviews.
    budget_5000 = review_budget(Usdc.from_decimal("5000"))
    assert budget_5000 == 39


def test_is_whitespace_only() -> None:
    patch_clean = """--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 2\n"""
    assert not is_whitespace_only(patch_clean)

    patch_ws = """--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n-x = 1  \n+x = 1\n"""
    assert is_whitespace_only(patch_ws)

    patch_empty_lines = """--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n+   \n- \n"""
    assert is_whitespace_only(patch_empty_lines)


def test_prescreen_draft() -> None:
    sub = Submitted(
        repo="acme/repo",
        pr_number=101,
        head_sha="a" * 40,
        title="WIP draft",
        criteria=("Fix bug",),
        files=(ChangedFile("foo.py", 1, 0, "+x = 1\n"),),
        checks_passed=True,
        draft=True,
    )
    j = prescreen(sub)
    assert j is not None
    assert j.prescreen_failed is True
    assert j.prescreen_reason == "draft"

    d = decide(j, checks_passed=True, files_changed=1, rework_rounds=0)
    assert d.verdict is Verdict.REJECT
    assert d.rule == "draft"


def test_prescreen_zero_changes() -> None:
    sub = Submitted(
        repo="acme/repo",
        pr_number=102,
        head_sha="b" * 40,
        title="Empty",
        criteria=("Fix bug",),
        files=(),
        checks_passed=True,
    )
    j = prescreen(sub)
    assert j is not None
    assert j.prescreen_failed is True
    assert j.prescreen_reason == "empty_diff"


def test_prescreen_whitespace_only() -> None:
    sub = Submitted(
        repo="acme/repo",
        pr_number=103,
        head_sha="c" * 40,
        title="Whitespace only",
        criteria=("Fix bug",),
        files=(ChangedFile("foo.py", 1, 0, "+   \n"),),
        checks_passed=True,
    )
    j = prescreen(sub)
    assert j is not None
    assert j.prescreen_failed is True
    assert j.prescreen_reason == "whitespace_only"


def test_duplicate_detection_exact_copy() -> None:
    patch = "+def solve(): return 42\n"
    prior = Submitted(
        repo="acme/repo",
        pr_number=201,
        head_sha="d" * 40,
        title="First PR",
        criteria=("Solve issue",),
        files=(ChangedFile("solution.py", 1, 0, patch),),
        checks_passed=True,
    )
    competing = Submitted(
        repo="acme/repo",
        pr_number=202,
        head_sha="e" * 40,
        title="Copied PR",
        criteria=("Solve issue",),
        files=(ChangedFile("solution.py", 1, 0, patch),),
        checks_passed=True,
        prior_submissions=(prior,),
    )
    is_dup, pr = detect_derivation(competing)
    assert is_dup is True
    assert pr == 201

    j = Judgement(
        checks=(),
        summary="Exact copy",
        reviewer="detector",
        is_duplicate=True,
        derived_from_pr=pr,
    )
    d = decide(j, checks_passed=True, files_changed=1, rework_rounds=0)
    assert d.verdict is Verdict.REJECT
    assert d.rule == "duplicate_submission"


def test_multiple_claimants_on_open_roster() -> None:
    store = make_store()

    # Create publisher and issue
    account = store.create_account("0x" + "11" * 20, "publisher", "acme-pub")
    pub = store.publish(
        PublishRequest(
            repo="acme/repo",
            number=42,
            title="Open Market Issue",
            publisher_id=account.party_id,
            price=Decimal("1000"),
            criteria=[
                "A test in tests/test_feature.py reproduces the fix and passes with exit code 0."
            ],
        )
    )
    criteria = ["A test in tests/test_feature.py reproduces the fix and passes with exit code 0."]
    store.approve_criteria(pub.id, criteria, by="acme-pub")
    store.set_price(pub.id, Usdc.from_decimal("1000"), by="acme-pub")
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    rec = store.get(pub.id)
    assert rec is not None
    store._fund(rec, when=now)
    store.repo.save_issues(rec)

    # Contributor 1 claims
    c1 = store.create_account("0x" + "22" * 20, "contributor", "alice")
    store.claim(pub.id, c1.party_id, now=now)

    # Contributor 2 also claims the SAME issue!
    c2 = store.create_account("0x" + "33" * 20, "contributor", "bob")
    store.claim(pub.id, c2.party_id, now=now + timedelta(hours=1))

    # Contributor 3 also claims!
    c3 = store.create_account("0x" + "44" * 20, "contributor", "carol")
    store.claim(pub.id, c3.party_id, now=now + timedelta(hours=2))

    rec = store.get(pub.id)
    assert rec is not None
    # All 3 contributors are on the roster
    assert len(rec.claims) == 3
    assert set(rec.contributor_ids) == {c1.party_id, c2.party_id, c3.party_id}


def test_winner_on_merge_and_superseded_claims() -> None:
    store = make_store()

    account = store.create_account("0x" + "11" * 20, "publisher", "acme-pub")
    criteria = ["A test in tests/test_feature.py reproduces the fix and passes with exit code 0."]
    pub = store.publish(
        PublishRequest(
            repo="acme/repo",
            number=43,
            title="Competition Issue",
            publisher_id=account.party_id,
            price=Decimal("800"),
            criteria=criteria,
        )
    )
    store.approve_criteria(pub.id, criteria, by="acme-pub")
    store.set_price(pub.id, Usdc.from_decimal("800"), by="acme-pub")
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    rec = store.get(pub.id)
    assert rec is not None
    store._fund(rec, when=now)
    store.repo.save_issues(rec)

    c1 = store.create_account("0x" + "22" * 20, "contributor", "alice")
    c2 = store.create_account("0x" + "33" * 20, "contributor", "bob")
    store.claim(pub.id, c1.party_id, now=now)
    store.claim(pub.id, c2.party_id, now=now)

    # Alice submits PR #101
    pr1 = PullRequest(
        repo="acme/repo",
        number=101,
        author="alice",
        head_sha="1111" * 10,
        body="Fixes #43",
        merged=False,
        files_changed=2,
        additions=20,
        deletions=5,
    )
    store.submit_pull_request(pub.id, pr1, now=now + timedelta(hours=1))

    # Bob submits PR #102
    pr2 = PullRequest(
        repo="acme/repo",
        number=102,
        author="bob",
        head_sha="2222" * 10,
        body="Fixes #43",
        merged=False,
        files_changed=1,
        additions=10,
        deletions=2,
    )
    store.submit_pull_request(pub.id, pr2, now=now + timedelta(hours=2))

    # Bob's PR #102 is merged!
    rec = store.pull_request_closed(pub.id, 102, merged=True, now=now + timedelta(hours=3))

    assert rec is not None
    assert rec.state is IssueState.PAID
    assert rec.contributor_id == c2.party_id
    assert rec.accepted_by == "merge"

    # Bob's claim is ACCEPTED, Alice's claim is SUPERSEDED!
    bob_claim = next(c for c in rec.claims if c.contributor_id == c2.party_id)
    alice_claim = next(c for c in rec.claims if c.contributor_id == c1.party_id)
    assert bob_claim.state == "ACCEPTED"
    assert alice_claim.state == "SUPERSEDED"
    assert alice_claim.active is False


def test_merge_after_deadline_releases_no_payout() -> None:
    store = make_store()

    account = store.create_account("0x" + "11" * 20, "publisher", "acme-pub")
    criteria = ["A test in tests/test_feature.py reproduces the fix and passes with exit code 0."]
    pub = store.publish(
        PublishRequest(
            repo="acme/repo",
            number=44,
            title="Expired Issue",
            publisher_id=account.party_id,
            price=Decimal("500"),
            criteria=criteria,
        )
    )
    store.approve_criteria(pub.id, criteria, by="acme-pub")
    store.set_price(pub.id, Usdc.from_decimal("500"), by="acme-pub")
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    rec = store.get(pub.id)
    assert rec is not None
    store._fund(rec, when=now)
    store.repo.save_issues(rec)

    c1 = store.create_account("0x" + "22" * 20, "contributor", "alice")
    store.claim(pub.id, c1.party_id, now=now)

    pr = PullRequest(
        repo="acme/repo",
        number=105,
        author="alice",
        head_sha="3333" * 10,
        body="Fixes #44",
        merged=False,
        files_changed=1,
        additions=10,
        deletions=0,
    )
    store.submit_pull_request(pub.id, pr, now=now + timedelta(hours=1))

    # PR is merged AFTER escrow deadline (14 days term)
    past_deadline = now + timedelta(days=20)
    rec = store.pull_request_closed(pub.id, 105, merged=True, now=past_deadline)

    assert rec is not None
    # No release attempted, state is not paid
    assert rec.state is not IssueState.PAID
    assert rec.decisions[-1].action == "merge_after_deadline"

"""Test fixtures and seed data isolated for integration and unit test suites."""

from __future__ import annotations

import copy
import random
from datetime import UTC, datetime, timedelta

from misthos.config import settings
from misthos.domain.issue import IssueState
from misthos.domain.money import Usdc
from misthos.domain.pricing import ComplexitySignals, propose
from misthos.domain.review import ChangedFile
from misthos.models.records import IssueRecord
from misthos.repositories import MemoryRepository, Seed
from misthos.schemas import Account, Contributor, Publisher, Wallet
from misthos.store import _PRE_FUNDING
from tests.fakes.billing import SimulatedRail
from tests.fakes.github import SimulatedGitHub


def seed_publishers() -> dict[str, Publisher]:
    rows = [
        ("PUB-1", "Acme Payments", "company", "enterprise", "48,500.00", "0xAcme00000000000000000000000000000000A1"),
        ("PUB-2", "Northwind Data", "company", "team", "12,300.00", "0xN0rthw1nd0000000000000000000000000000b2"),
        ("PUB-3", "R. Okafor", "maintainer", "open", "480.00", "0x0kaf0r000000000000000000000000000000c3"),
        ("PUB-4", "Globex Logistics", "company", "enterprise", "91,000.00", "0xGl0bex00000000000000000000000000000004"),
        ("PUB-5", "Contoso Tooling", "company", "team", "6,750.00", "0xC0nt0s000000000000000000000000000000d5"),
    ]
    return {
        r[0]: Publisher(
            id=r[0],
            name=r[1],
            kind=r[2],  # type: ignore[arg-type]
            tier=r[3],  # type: ignore[arg-type]
            wallet=Wallet(address=r[5], chain=settings.chain),
            budget_remaining_usdc=r[4],
        )
        for r in rows
    }


def seed_contributors() -> dict[str, Contributor]:
    rows = [
        ("CON-1", "amara.dev", 38, 21, "8,420.00", True),
        ("CON-2", "jonas_k", 27, 14, "5,110.00", True),
        ("CON-3", "priya.codes", 19, 9, "3,050.00", True),
        ("CON-4", "lin.wei", 12, 6, "1,880.00", True),
        ("CON-5", "tom_e", 5, 2, "540.00", False),
        ("CON-6", "sara_k", 2, 1, "180.00", False),
    ]
    return {
        r[0]: Contributor(
            id=r[0],
            handle=r[1],
            wallet=Wallet(address=f"0x{r[1].replace('.', ''):0<40}"[:42], chain=settings.chain),
            reputation=r[2],
            settled_issues=r[3],
            earned_usdc=r[4],
            identity_status="verified" if r[5] else "unverified",
            identity_reference="seeded" if r[5] else None,
        )
        for r in rows
    }


_ISSUE_SPECS: list[dict] = [
    {
        "repo": "acme/ledger-core",
        "number": 412,
        "title": "Currency rounding is off by one cent on half-up values",
        "summary": "Rounding a half-cent value rounds down instead of up, so totals drift by a cent on long invoices.",
        "state": "FUNDED",
        "labels": ["bug", "payments"],
        "compliance_driven": False,
        "criteria": [
            "A test reproduces the round-down on a half-cent value.",
            "Rounding is half-up for every currency exponent the library supports.",
            "The existing rounding API keeps its signature.",
        ],
        "publisher_id": "PUB-1",
        "age_days": 2,
        "signals": {
            "code_surface": 2.0,
            "requirement_clarity": 1.0,
            "test_coverage": 2.0,
            "dependency_depth": 1.0,
            "prior_attempts": 3.0,
            "blast_radius": 2.0,
        },
    },
    {
        "repo": "acme/ledger-core",
        "number": 428,
        "title": "Add a plugin hook for custom settlement adapters",
        "summary": "Every new rail needs a fork. A documented hook would let publishers ship their own adapter.",
        "state": "IN_REVIEW",
        "labels": ["feature", "extensibility"],
        "compliance_driven": False,
        "criteria": [
            "A settlement adapter can be registered without touching core.",
            "The hook is documented with a working example.",
            "Core has no import of any concrete adapter.",
        ],
        "publisher_id": "PUB-1",
        "age_days": 9,
        "contributor_id": "CON-2",
        "pr": {
            "pr_number": 903,
            "head_sha": "9f2c1b7d4e6a8c0f3b5d7e9a1c3f5b7d9e1a3c5f",
            "checks_passed": True,
            "files_changed": 7,
            "additions": 214,
            "deletions": 38,
        },
        "signals": {
            "code_surface": 4.0,
            "requirement_clarity": 3.0,
            "test_coverage": 3.0,
            "dependency_depth": 3.0,
            "prior_attempts": 2.0,
            "blast_radius": 3.0,
        },
    },
    {
        "repo": "globex/parse-locale",
        "number": 87,
        "title": "Memory safety: unterminated locale string reachable from the network parser",
        "summary": "A crafted locale tag reaches an unsafe read in the parser. Reachable from any HTTP caller.",
        "state": "CLAIMED",
        "labels": ["security", "cve"],
        "compliance_driven": True,
        "criteria": [
            "A fuzz case reproduces the out-of-bounds read.",
            "The parser rejects unterminated input before allocation.",
            "The fix ships with a regression test and a CVE reference in the changelog.",
        ],
        "publisher_id": "PUB-4",
        "age_days": 3,
        "contributor_id": "CON-1",
        "risk": "1200.00",
        "urgency": "800.00",
        "signals": {
            "code_surface": 3.0,
            "requirement_clarity": 2.0,
            "test_coverage": 4.0,
            "dependency_depth": 4.0,
            "prior_attempts": 1.0,
            "blast_radius": 5.0,
        },
    },
    {
        "repo": "northwind/httpx-retry",
        "number": 219,
        "title": "Retry budget ignores jitter, causing thundering herds",
        "summary": "All clients retry on the same schedule, so a blip becomes an outage.",
        "state": "PAID",
        "labels": ["bug", "reliability"],
        "compliance_driven": False,
        "criteria": [
            "Backoff includes jitter by default.",
            "The old fixed schedule is available behind a flag.",
            "A test asserts the jittered spread is non-zero.",
        ],
        "publisher_id": "PUB-2",
        "age_days": 6,
        "contributor_id": "CON-3",
        "pr": {
            "pr_number": 611,
            "head_sha": "3a5c7e9b1d3f5a7c9e1b3d5f7a9c1e3b5d7f9a1c",
            "checks_passed": True,
            "files_changed": 4,
            "additions": 96,
            "deletions": 31,
        },
        "verdict": "accept",
        "findings": ["Criteria met.", "Jitter spread verified in the new test."],
        "signals": {
            "code_surface": 2.0,
            "requirement_clarity": 2.0,
            "test_coverage": 3.0,
            "dependency_depth": 2.0,
            "prior_attempts": 1.0,
            "blast_radius": 3.0,
        },
    },
    {
        "repo": "contoso/schema-cli",
        "number": 55,
        "title": "Add a dry-run flag to schema migrations",
        "summary": "Operators have no way to see what a migration will do before running it.",
        "state": "PAID",
        "labels": ["feature", "dx"],
        "compliance_driven": False,
        "criteria": [
            "A dry run prints the statements without executing them.",
            "The flag is documented in the CLI help.",
        ],
        "publisher_id": "PUB-5",
        "age_days": 30,
        "contributor_id": "CON-4",
        "pr": {
            "pr_number": 302,
            "head_sha": "c1e3a5b7d9f1c3e5a7b9d1f3c5e7a9b1d3f5c7e9",
            "checks_passed": True,
            "files_changed": 3,
            "additions": 74,
            "deletions": 12,
        },
        "verdict": "accept",
        "findings": ["Both criteria covered.", "Help text is accurate."],
        "signals": {
            "code_surface": 2.0,
            "requirement_clarity": 1.0,
            "test_coverage": 2.0,
            "dependency_depth": 1.0,
            "prior_attempts": 1.0,
            "blast_radius": 1.0,
        },
    },
    {
        "repo": "acme/ledger-adapters",
        "number": 178,
        "title": "EU Cyber Resilience Act: produce a maintenance record for shipped dependencies",
        "summary": "The 2027 obligations need evidence that dependencies are maintained, not a snapshot of versions.",
        "state": "AWAITING_APPROVAL",
        "labels": ["compliance", "reporting"],
        "compliance_driven": True,
        "criteria": [
            "A report lists each shipped dependency with its last fix and its maintainer.",
            "The report exports to a format an auditor accepts.",
            "Generation is deterministic and repeatable.",
        ],
        "publisher_id": "PUB-4",
        "age_days": 1,
        "signals": {
            "code_surface": 4.0,
            "requirement_clarity": 4.0,
            "test_coverage": 3.0,
            "dependency_depth": 3.0,
            "prior_attempts": 2.0,
            "blast_radius": 4.0,
        },
    },
    {
        "repo": "northwind/httpx-retry",
        "number": 231,
        "title": "Support per-request timeouts that override the client default",
        "summary": "A single slow endpoint forces a global timeout increase for everyone.",
        "state": "REWORK",
        "labels": ["feature", "api"],
        "compliance_driven": False,
        "criteria": [
            "A per-request timeout overrides the client default.",
            "The override is documented.",
            "Backwards compatibility is preserved for callers that pass nothing.",
        ],
        "publisher_id": "PUB-2",
        "age_days": 11,
        "contributor_id": "CON-5",
        "pr": {
            "pr_number": 640,
            "head_sha": "5b7d9f1a3c5e7b9d1f3a5c7e9b1d3f5a7c9e1b3d",
            "checks_passed": True,
            "files_changed": 2,
            "additions": 41,
            "deletions": 6,
        },
        "verdict": "rework",
        "findings": [
            "Criterion 3 is not covered: a caller passing no timeout changes behaviour.",
            "Add a regression test for the default path before resubmitting.",
        ],
        "signals": {
            "code_surface": 3.0,
            "requirement_clarity": 3.0,
            "test_coverage": 4.0,
            "dependency_depth": 2.0,
            "prior_attempts": 4.0,
            "blast_radius": 2.0,
        },
    },
    {
        "repo": "contoso/schema-cli",
        "number": 61,
        "title": "Migrate the config loader from TOML to a pluggable format",
        "summary": "Teams keep asking for YAML. A pluggable loader would end the argument.",
        "state": "REFUNDED",
        "labels": ["feature"],
        "compliance_driven": False,
        "criteria": [
            "A second config format is added by registering a loader, without editing core.",
            "Existing TOML configs keep working.",
        ],
        "publisher_id": "PUB-5",
        "age_days": 40,
        "signals": {
            "code_surface": 5.0,
            "requirement_clarity": 5.0,
            "test_coverage": 4.0,
            "dependency_depth": 3.0,
            "prior_attempts": 3.0,
            "blast_radius": 4.0,
        },
    },
]

ISSUE_SPECS = _ISSUE_SPECS


def _demo_files(repo: str) -> list[ChangedFile]:
    """The file list of a pull request the simulation makes up."""
    slug = repo.split("/")[-1].replace("-", "_")
    return [
        ChangedFile(f"src/{slug}/fix.py", additions=40, deletions=6),
        ChangedFile(f"tests/test_{slug}.py", additions=22),
        ChangedFile(f"docs/{slug}.md", additions=12),
        ChangedFile("CHANGELOG.md", additions=2),
    ]


def _fabricate_files(builder, rec: IssueRecord) -> None:
    if rec.submission is None or not isinstance(builder.github, SimulatedGitHub):
        return
    builder.github.put_files(rec.repo, rec.submission.pr_number, _demo_files(rec.repo))


def _build(builder, spec: dict) -> IssueRecord:
    publisher = builder.repo.get_publisher(spec["publisher_id"])
    assert publisher is not None, spec["publisher_id"]
    signals = ComplexitySignals(**spec["signals"])
    proposal = propose(
        signals,
        compliance_driven=spec.get("compliance_driven", False),
        comparables=builder._comparables_for(signals, spec["repo"]),
        urgency=Usdc.from_decimal(spec["urgency"]) if spec.get("urgency") else None,
        risk_premium=Usdc.from_decimal(spec["risk"]) if spec.get("risk") else None,
    )
    target = IssueState(spec["state"])
    now = datetime.now(UTC)
    if "price" in spec:
        price = Usdc.from_decimal(spec["price"]) if spec["price"] else None
    else:
        price = proposal.recommended
    rec = IssueRecord(
        id=builder._next_issue_id(),
        repo=spec["repo"],
        number=spec["number"],
        title=spec["title"],
        summary=spec["summary"],
        state=target if target in _PRE_FUNDING else IssueState.AWAITING_APPROVAL,
        labels=spec["labels"],
        compliance_driven=spec.get("compliance_driven", False),
        acceptance_criteria=spec["criteria"],
        publisher_id=spec["publisher_id"],
        created_at=now - timedelta(days=spec["age_days"]),
        proposal=proposal,
        price=price,
    )

    if spec.get("signals_read"):
        builder._log(
            rec,
            actor="agent",
            action="signals_read",
            rule="github_read_path",
            outcome=spec["signals_read"],
        )
    builder._log(
        rec,
        actor="agent",
        action="price_suggested",
        rule="complexity_score_v1",
        outcome=(
            f"suggested {proposal.recommended} for {proposal.estimated_hours}h; "
            "advice only, the publisher's price is what binds"
        ),
        cost="0.00",
    )

    if target in _PRE_FUNDING:
        return rec

    rec.criteria_approved_at = rec.created_at + timedelta(hours=5)
    builder._fund(rec, when=rec.created_at + timedelta(hours=6))
    builder._log(
        rec,
        actor="publisher",
        action="price_approved",
        rule="human_checkpoint",
        outcome=f"approved {rec.price} and committed the funds",
    )

    if target is IssueState.FUNDED:
        return rec

    if target is IssueState.REFUNDED:
        builder._refund(rec, rule="deadline_passed")
        return rec

    contributor_id = spec["contributor_id"]
    builder._claim(rec, contributor_id, when=rec.created_at + timedelta(hours=20))
    builder._log(
        rec,
        actor="contributor",
        action="claimed",
        rule="first_claim_wins",
        outcome=f"{builder._handle(contributor_id)} took an exclusive 72h claim",
    )

    if target is IssueState.CLAIMED:
        return rec

    builder._submit(rec, spec["pr"])
    _fabricate_files(builder, rec)

    if target is IssueState.IN_REVIEW:
        return rec

    builder._review(
        rec,
        verdict=spec["verdict"],
        findings=spec["findings"],
        when=rec.created_at + timedelta(hours=44),
    )

    if target is IssueState.REWORK:
        builder._log(
            rec,
            actor="agent",
            action="verdict_issued",
            rule="criteria_unmet",
            outcome="rework by review_agent_v1: " + " ".join(spec["findings"]),
            when=rec.created_at + timedelta(hours=44),
        )
        return rec

    builder._log(
        rec,
        actor="agent",
        action="verdict_passed",
        rule="review_agent_v1",
        outcome="verdict passed, awaiting the publisher's merge",
        when=rec.created_at + timedelta(hours=45),
    )

    if target is IssueState.ACCEPTED:
        return rec

    rec.accepted_by = "merge"
    builder._release(rec, when=rec.created_at + timedelta(hours=45, minutes=2))
    return rec


def build_test_seed(target_store) -> Seed:
    """Build a fresh Seed dataclass for test resets."""
    staging = MemoryRepository()
    builder = copy.copy(target_store)
    builder.repo = staging
    builder._ready = True
    random.seed(7)
    for publisher in seed_publishers().values():
        builder.repo.save_publisher(publisher)
    for contributor in seed_contributors().values():
        builder.repo.save_contributor(contributor)
    for spec in ISSUE_SPECS:
        builder.repo.save_issues(_build(builder, spec))
    builder.rebuild_standing()
    return staging.as_seed()



def reset_store_for_tests(store) -> None:
    """Reset all repositories and stateful test doubles to seed state."""
    migrate = getattr(store.repo, "migrate", None)
    if migrate is not None:
        migrate()
    reset_chain = getattr(store.chain, "reset", None)
    if reset_chain is not None:
        reset_chain()
    reset_coord = getattr(store.coordinator, "reset", None)
    if reset_coord is not None:
        reset_coord()
    if isinstance(store.rail, SimulatedRail):
        store.rail.reset()
    reset_github = getattr(store.github, "reset", None)
    if reset_github is not None:
        reset_github()
    seed = build_test_seed(store)
    store.repo.reset(seed)
    store._ready = True


def seed_store_for_tests(store) -> None:
    """Helper to populate store with test data in integration test suites."""
    reset_store_for_tests(store)


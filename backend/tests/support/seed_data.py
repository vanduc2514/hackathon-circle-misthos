# ruff: noqa: E501
"""The demo data the suite starts from. Test-only: nothing here ships."""

from __future__ import annotations

from misthos.config import settings
from misthos.schemas import Contributor, Publisher, Wallet

# ------------------------------------------------------------------ seed data


def _seed_publishers() -> dict[str, Publisher]:
    rows = [
        (
            "PUB-1",
            "Acme Payments",
            "company",
            "enterprise",
            "48,500.00",
            "0xAcme00000000000000000000000000000000A1",
        ),
        (
            "PUB-2",
            "Northwind Data",
            "company",
            "team",
            "12,300.00",
            "0xN0rthw1nd0000000000000000000000000000b2",
        ),
        (
            "PUB-3",
            "R. Okafor",
            "maintainer",
            "open",
            "480.00",
            "0x0kaf0r000000000000000000000000000000c3",
        ),
        (
            "PUB-4",
            "Globex Logistics",
            "company",
            "enterprise",
            "91,000.00",
            "0xGl0bex00000000000000000000000000000004",
        ),
        (
            "PUB-5",
            "Contoso Tooling",
            "company",
            "team",
            "6,750.00",
            "0xC0nt0s000000000000000000000000000000d5",
        ),
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


def _seed_contributors() -> dict[str, Contributor]:
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
            # Seeded contributors who have been paid before were verified then.
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
        # Claimed 52 hours ago, so the 72-hour claim is still live when the demo
        # starts and the sweeper only returns it to the pool if nobody acts for a day.
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
        # Paid four days before the seed, so a fresh demo has a settlement this week:
        # the dashboard leads with that number.
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

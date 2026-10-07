"""#38: drafted criteria are checkable, and uncheckable ones are refused."""

from __future__ import annotations

import pytest

from misthos.domain.criteria import MAX_CRITERIA, draft, problems
from misthos.domain.review import ChangedFile, Submitted
from misthos.services.review.rules import RuleReviewer
from misthos.store import _ISSUE_SPECS

# Twenty issue shapes, the sample #38 asks a person to judge.
SHAPES = [
    ("Currency rounding is off by one cent on half-up values",
     "In `src/money/round.py` the half-up branch truncates.", ["bug", "payments"]),
    ("Add a plugin hook for custom settlement adapters", "", ["feature"]),
    ("Memory safety: unterminated locale string reachable from the network parser",
     "The read in parser/locale.c runs past the buffer.", ["security", "cve"]),
    ("Retry budget ignores jitter, causing thundering herds", "", ["bug", "reliability"]),
    ("Add a dry-run flag to schema migrations", "", ["feature", "dx"]),
    ("Support per-request timeouts that override the client default", "", ["feature", "api"]),
    ("Search is slow on large repositories", "", []),
    ("Document the retry configuration", "", ["docs"]),
    ("Crash when the config file is empty",
     "- [ ] An empty `config.toml` loads with defaults\n- [ ] works properly", ["bug"]),
    ("Upgrade the YAML parser to drop a CVE-affected version", "", ["security"]),
    ("Expose the queue depth as a Prometheus metric", "", []),
    ("Login form loses the redirect after a failed attempt",
     "Happens in web/login.tsx and api/session.py", ["bug"]),
    ("Implement pagination on the issues endpoint", "", ["enhancement"]),
    ("CSV export breaks on commas inside fields", "", ["bug"]),
    ("Startup takes ten seconds because plugins load eagerly", "", ["performance"]),
    ("README install steps are out of date for Windows", "", ["documentation"]),
    ("Allow a custom User-Agent header", "", []),
    ("Timezone offset is applied twice to scheduled jobs",
     "See `scheduler/cron.py` and `scheduler/tz.py`.", ["bug"]),
    ("SQL injection through the sort parameter", "", ["bug"]),
    ("Deprecation warnings flood the logs on Python 3.13", "", []),
]  # fmt: skip


class TestRefusingWhatCannotBeJudged:
    @pytest.mark.parametrize(
        ("criterion", "reason"),
        [
            ("Is it fast?", "question"),
            ("Loader is pluggable.", "too short"),
            ("Rounding works correctly in every case we care about.", '"works"'),
            ("The UI is cleaner and more intuitive for users.", '"cleaner"'),
            ("It parses, validates and saves and reports and retries.", "several things"),
            ("x" * 301, "longer than"),
        ],
    )
    def test_refuses(self, criterion: str, reason: str) -> None:
        (problem,) = problems([criterion])
        assert reason in problem.reason

    def test_a_repeat_is_refused(self) -> None:
        found = problems(["A test covers the 503.", "a test covers the 503"])
        assert [(p.index, p.reason) for p in found] == [(2, "repeats criterion 1")]

    def test_more_than_a_dozen_is_a_project(self) -> None:
        many = [f"A test covers case number {i}." for i in range(MAX_CRITERIA + 1)]
        assert "split it" in problems(many)[0].reason

    @pytest.mark.parametrize(
        "criterion",
        [
            "Backoff includes jitter by default.",
            "Rounding works correctly for `0.005` and `-0.005`.",
            "Requests over the limit return 429 within 50 ms.",
            "The parser rejects unterminated input before allocation.",
            "Existing TOML configs keep working.",
            "Generation is deterministic and repeatable.",
            "The old fixed schedule is available behind a flag.",
            "A fuzz case reproduces the out-of-bounds read.",
        ],
    )
    def test_specific_prose_passes(self, criterion: str) -> None:
        assert problems([criterion]) == []

    def test_every_seeded_criterion_passes(self) -> None:
        for spec in _ISSUE_SPECS:
            assert problems(spec["criteria"]) == [], spec["title"]


class TestDrafting:
    @pytest.mark.parametrize(("title", "body", "labels"), SHAPES)
    def test_every_draft_passes_its_own_rules(
        self, title: str, body: str, labels: list[str]
    ) -> None:
        drafted = draft(title, body, labels)
        assert 2 <= len(drafted) <= MAX_CRITERIA
        assert problems(drafted) == []

    def test_twenty_shapes_make_the_sample(self) -> None:
        assert len(SHAPES) == 20

    def test_files_the_issue_names_become_a_criterion(self) -> None:
        drafted = draft(*SHAPES[17])
        assert any("`scheduler/cron.py`" in c and "`scheduler/tz.py`" in c for c in drafted)

    def test_the_publishers_task_list_is_kept_when_checkable(self) -> None:
        drafted = draft(*SHAPES[8])
        assert drafted[0] == "An empty `config.toml` loads with defaults."
        assert not any("properly" in c for c in drafted)

    def test_kinds_get_the_criteria_that_fit_them(self) -> None:
        security = " ".join(draft(*SHAPES[2]))
        assert "regression test" in security and "versions it affects" in security
        docs = draft(*SHAPES[7])
        assert not any("changelog" in c.lower() for c in docs)
        feature = " ".join(draft(*SHAPES[1]))
        assert "docs" in feature and "edge case" in feature
        upgrade = " ".join(draft(*SHAPES[9]))
        assert "lock file" in upgrade and "versions it affects" in upgrade
        assert "regression test" not in upgrade

    @pytest.mark.parametrize(("title", "body", "labels"), SHAPES)
    def test_a_complete_pull_request_meets_every_judgeable_draft(
        self, title: str, body: str, labels: list[str]
    ) -> None:
        """A fix with its test, docs and changelog meets what the rule reviewer can judge,
        so drafting never asks for something no pull request could show."""
        judged = RuleReviewer().judge(
            Submitted(
                repo="acme/x",
                pr_number=1,
                head_sha="a" * 40,
                title=title,
                criteria=tuple(draft(title, body, labels)),
                files=(
                    ChangedFile("src/x/fix.py", 40, 6),
                    ChangedFile("tests/test_x.py", 22),
                    ChangedFile("docs/x.md", 12),
                    ChangedFile("CHANGELOG.md", 2),
                ),
                checks_passed=True,
            )
        )
        assert all(c.met is not False for c in judged.checks), judged.checks

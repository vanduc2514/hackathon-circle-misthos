"""How a verdict follows from a judgement, and what the rule reviewer can judge."""

from __future__ import annotations

import pytest

from misthos.domain.review import (
    MAX_REWORK_ROUNDS,
    ChangedFile,
    CriterionCheck,
    Judgement,
    Submitted,
    Verdict,
    decide,
)
from misthos.services.review.rules import RuleReviewer


def judged(*met: bool | None) -> Judgement:
    checks = tuple(CriterionCheck(i, f"criterion {i}", m, "why") for i, m in enumerate(met, 1))
    return Judgement(checks=checks, summary="", reviewer="test")


class TestDecide:
    def test_everything_met_and_green_is_accepted(self) -> None:
        d = decide(judged(True, True), checks_passed=True, files_changed=2, rework_rounds=0)
        assert d.verdict is Verdict.ACCEPT and d.rule == "criteria_met"

    def test_an_unjudged_criterion_does_not_block_but_is_said(self) -> None:
        d = decide(judged(True, None), checks_passed=True, files_changed=2, rework_rounds=0)
        assert d.verdict is Verdict.ACCEPT
        assert any("not judged" in f for f in d.findings)

    def test_unmet_criteria_are_restated_for_the_rework(self) -> None:
        d = decide(judged(False, True, False), checks_passed=True, files_changed=2, rework_rounds=0)
        assert d.verdict is Verdict.REWORK and d.rule == "criteria_unmet"
        assert d.findings[-1] == "To be accepted, criteria 1 and 3 must be met."

    def test_failing_checks_are_rework_even_when_criteria_are_met(self) -> None:
        d = decide(judged(True), checks_passed=False, files_changed=2, rework_rounds=0)
        assert d.verdict is Verdict.REWORK and d.rule == "checks_failing"

    def test_rework_is_bounded(self) -> None:
        last = decide(
            judged(False), checks_passed=True, files_changed=2, rework_rounds=MAX_REWORK_ROUNDS
        )
        assert last.verdict is Verdict.REJECT and last.rule == "rework_rounds_exhausted"
        assert "criteria 1 are still unmet" in last.findings[-1]

    def test_a_pull_request_that_changes_nothing_is_rejected(self) -> None:
        d = decide(judged(True), checks_passed=True, files_changed=0, rework_rounds=0)
        assert d.verdict is Verdict.REJECT and d.rule == "empty_diff"


def submitted(criteria: list[str], paths: list[str]) -> Submitted:
    return Submitted(
        repo="a/b",
        pr_number=1,
        head_sha="0" * 40,
        title="t",
        criteria=tuple(criteria),
        files=tuple(ChangedFile(p) for p in paths),
        checks_passed=True,
    )


class TestRuleReviewer:
    @pytest.mark.parametrize(
        ("criterion", "paths", "met"),
        [
            ("A test reproduces the bug.", ["src/a.py", "tests/test_a.py"], True),
            ("A test reproduces the bug.", ["src/a.py"], False),
            ("Documented in the changelog.", ["CHANGELOG.md"], True),
            ("Documented in the changelog.", ["docs/guide.md"], False),
            ("The hook is documented with a working example.", ["docs/hooks.md"], True),
            ("The hook is documented with a working example.", ["CHANGELOG.md"], False),
            ("A regression test and a changelog line.", ["tests/test_x.py"], False),
            ("A regression test and a changelog line.", ["tests/test_x.py", "CHANGES.rst"], True),
            ("Locale strings are validated before use.", ["src/a.py"], None),
        ],
    )
    def test_judges_what_a_file_list_can_prove(
        self, criterion: str, paths: list[str], met: bool | None
    ) -> None:
        check = RuleReviewer().judge(submitted([criterion], paths)).checks[0]
        assert check.met is met

    def test_costs_nothing_and_says_what_it_saw(self) -> None:
        judgement = RuleReviewer().judge(submitted(["A test."], ["tests/test_a.py", "a.py"]))
        assert judgement.cost.base_units == 0
        assert judgement.summary == "1 of 1 judgeable criteria met across 2 changed files"
        assert "`tests/test_a.py`" in judgement.checks[0].evidence

"""A reviewer made of rules, for when no model is configured.

It judges only what a file list can prove: a criterion that asks for a test is met
when the pull request changes a test, and one that asks for documentation or a
changelog entry is met when it changes one. Everything else needs someone to read
the code, so it is left unjudged and the findings say so. It never calls anything,
costs nothing, and gives the same answer every time, which makes it the baseline
the regression corpus pins and the reviewer the simulation runs.
"""

from __future__ import annotations

import re

from misthos.domain.review import CriterionCheck, Judgement, Submitted
from misthos.domain.signals import is_changelog_path, is_docs_path, is_test_path

_ASKS = (
    ("a test", re.compile(r"\b(tests?|tested|fuzz)\b", re.IGNORECASE), is_test_path),
    ("a changelog entry", re.compile(r"\bchangelog\b", re.IGNORECASE), is_changelog_path),
    (
        "documentation",
        re.compile(r"\b(documented|documentation|docs|readme|example)\b", re.IGNORECASE),
        is_docs_path,
    ),
)


def _names(paths: list[str], limit: int = 3) -> str:
    shown = ", ".join(f"`{p}`" for p in paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


class RuleReviewer:
    name = "rules_v1"

    def judge(self, submitted: Submitted) -> Judgement:
        paths = [f.path for f in submitted.files]

        checks: list[CriterionCheck] = []
        for index, criterion in enumerate(submitted.criteria, start=1):
            asks = [(what, [p for p in paths if kind(p)]) for what, rule, kind in _ASKS
                    if rule.search(criterion)]  # fmt: skip
            # "Documented in the changelog" asks for a changelog entry, not docs.
            if any(what == "a changelog entry" for what, _ in asks):
                asks = [(w, found) for w, found in asks if w != "documentation"]
            if not asks:
                met: bool | None = None
                evidence = "needs the code read; the rule reviewer does not judge it"
            else:
                missing = [what for what, found in asks if not found]
                met = not missing
                evidence = (
                    "changes " + _names([p for _, found in asks for p in found])
                    if met
                    else "asks for " + " and ".join(missing) + ", and the pull request has none"
                )
            checks.append(CriterionCheck(index, criterion, met, evidence))

        judged = [c for c in checks if c.met is not None]
        summary = (
            f"{sum(1 for c in judged if c.met)} of {len(judged)} judgeable criteria met "
            f"across {len(paths)} changed files"
        )
        return Judgement(checks=tuple(checks), summary=summary, reviewer=self.name)

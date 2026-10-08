"""How a verdict follows from a review.

A reviewer, the model or the rules, judges each acceptance criterion against the pull
request and says why. The verdict is not the reviewer's to pick: it follows from
those judgements, the project's own checks and how many rework rounds have already
run, by the rules below, so the same judgements always produce the same verdict and
anyone can see which rule produced it.

- An empty pull request is rejected.
- Every criterion met, or not contradicted, and the checks passing: accepted.
- Anything unmet, or failing checks: rework, restating exactly what is unmet.
- Still unmet after `MAX_REWORK_ROUNDS` rounds: rejected. An unbounded loop costs
  more than the fix is worth, which is the problem the product exists to solve.

A criterion the reviewer could not judge does not block acceptance on its own, and
the findings say it was not judged, so the publisher knows what to look at.

Checks that have not reported are not failing checks. They may still be running, so
a submission waits `CHECKS_WAIT` for them before it is reviewed; but a repository
with no CI never reports, and reading that as failing would send its work back for
rework forever. After the wait the work is judged on the criteria alone, and the
findings say the checks had not reported.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from misthos.domain.money import Usdc

MAX_REWORK_ROUNDS = 2
# Long enough for a typical CI run to finish, short enough that a repository with no
# CI is not kept waiting for its review much longer than it takes to read the diff.
CHECKS_WAIT = timedelta(minutes=30)


class Verdict(StrEnum):
    ACCEPT = "accept"
    REWORK = "rework"
    REJECT = "reject"


@dataclass(frozen=True)
class ChangedFile:
    path: str
    additions: int = 0
    deletions: int = 0
    patch: str = ""
    """The unified diff, when the reader has it. GitHub omits it for very large files."""


@dataclass(frozen=True)
class Submitted:
    """What a reviewer is given: the published criteria, and the pull request to hold
    against them."""

    repo: str
    pr_number: int
    head_sha: str
    title: str
    criteria: tuple[str, ...]
    files: tuple[ChangedFile, ...]
    checks_passed: bool | None
    """None when the project's checks have not reported on this commit."""


@dataclass(frozen=True)
class CriterionCheck:
    index: int
    """1-based, in the order the criteria were published."""
    criterion: str
    met: bool | None
    """None when the reviewer could not judge it."""
    evidence: str


@dataclass(frozen=True)
class Judgement:
    """What one reviewer concluded, and what concluding it cost."""

    checks: tuple[CriterionCheck, ...]
    summary: str
    reviewer: str
    cost: Usdc = Usdc(0)


@dataclass(frozen=True)
class Decided:
    verdict: Verdict
    findings: list[str]
    rule: str
    """The rule above that produced the verdict, for the decision log."""


def ready_for_review(
    checks_passed: bool | None, submitted_at: datetime | None, now: datetime
) -> bool:
    """Whether a submitted commit is reviewed now, or waits for its checks to report.
    A submission whose time is not known has waited long enough."""
    return checks_passed is not None or submitted_at is None or now >= submitted_at + CHECKS_WAIT


def decide(
    judgement: Judgement,
    *,
    checks_passed: bool | None,
    files_changed: int,
    rework_rounds: int,
) -> Decided:
    if files_changed == 0:
        return Decided(Verdict.REJECT, ["The pull request changes no files."], "empty_diff")

    findings = [_line(c) for c in judgement.checks]
    if checks_passed is False:
        findings.append("The project's own checks do not pass on this commit.")
    elif checks_passed is None:
        findings.append(
            "The project's checks had not reported on this commit, so it was judged on the "
            "criteria alone."
        )
    unmet = [c.index for c in judgement.checks if c.met is False]

    if not unmet and checks_passed is not False:
        rule = "criteria_met" if checks_passed else "criteria_met_checks_unreported"
        return Decided(Verdict.ACCEPT, findings, rule)
    if rework_rounds >= MAX_REWORK_ROUNDS:
        findings.append(
            f"This is the end of {rework_rounds} rework rounds"
            + (f" and criteria {_list(unmet)} are still unmet" if unmet else "")
            + ", so the pull request is rejected."
        )
        return Decided(Verdict.REJECT, findings, "rework_rounds_exhausted")
    if unmet:
        findings.append(f"To be accepted, criteria {_list(unmet)} must be met.")
    return Decided(Verdict.REWORK, findings, "criteria_unmet" if unmet else "checks_failing")


def _line(check: CriterionCheck) -> str:
    state = {True: "met", False: "not met", None: "not judged"}[check.met]
    return f"Criterion {check.index} {state}: {check.evidence}"


def _list(indexes: Sequence[int]) -> str:
    if len(indexes) == 1:
        return str(indexes[0])
    return ", ".join(str(i) for i in indexes[:-1]) + f" and {indexes[-1]}"

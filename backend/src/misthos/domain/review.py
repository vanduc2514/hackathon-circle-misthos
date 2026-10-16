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
    draft: bool = False
    prior_submissions: tuple[Submitted, ...] = ()


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
    prescreen_failed: bool = False
    prescreen_reason: str | None = None
    is_duplicate: bool = False
    derived_from_pr: int | None = None


def is_whitespace_only(patch: str) -> bool:
    """Return True if unified diff patch contains only whitespace additions/deletions."""
    adds = []
    dels = []
    all_empty = True
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            stripped = "".join(line[1:].split())
            if stripped:
                all_empty = False
            adds.append(stripped)
        elif line.startswith("-") and not line.startswith("---"):
            stripped = "".join(line[1:].split())
            if stripped:
                all_empty = False
            dels.append(stripped)
    return all_empty or (adds == dels)


def prescreen(submitted: Submitted) -> Judgement | None:
    """Pre-screen submission before running review inference."""
    if submitted.draft:
        return Judgement(
            checks=(),
            summary="Draft pull requests are not reviewed.",
            reviewer="prescreen",
            prescreen_failed=True,
            prescreen_reason="draft",
        )
    if not submitted.files or all(f.additions == 0 and f.deletions == 0 for f in submitted.files):
        return Judgement(
            checks=(),
            summary="The pull request changes no files.",
            reviewer="prescreen",
            prescreen_failed=True,
            prescreen_reason="empty_diff",
        )
    # Check if whitespace only: only when patches exist and every patch is whitespace-only
    has_patches = any(bool(f.patch) for f in submitted.files)
    if has_patches and all(not f.patch or is_whitespace_only(f.patch) for f in submitted.files):
        return Judgement(
            checks=(),
            summary="The pull request contains only whitespace changes.",
            reviewer="prescreen",
            prescreen_failed=True,
            prescreen_reason="whitespace_only",
        )
    return None


def detect_derivation(submitted: Submitted) -> tuple[bool, int | None]:
    """Check against prior competing submissions on the same issue.

    Returns (is_duplicate, derived_from_pr).
    """
    if not submitted.prior_submissions or not submitted.files:
        return False, None

    curr_patches = {f.path: f.patch.strip() for f in submitted.files if f.patch}
    if not curr_patches:
        return False, None

    for prior in submitted.prior_submissions:
        if prior.pr_number == submitted.pr_number:
            continue
        prior_patches = {f.path: f.patch.strip() for f in prior.files if f.patch}
        if not prior_patches:
            continue

        if curr_patches == prior_patches:
            return True, prior.pr_number

        common_files = set(curr_patches.keys()) & set(prior_patches.keys())
        if common_files and len(common_files) >= 0.8 * len(curr_patches):
            matching = sum(1 for fn in common_files if curr_patches[fn] == prior_patches[fn])
            if matching >= 0.8 * len(curr_patches):
                return False, prior.pr_number

    return False, None


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
    if judgement.prescreen_failed:
        return Decided(Verdict.REJECT, [judgement.summary], judgement.prescreen_reason or "prescreen_failed")

    if judgement.is_duplicate:
        return Decided(
            Verdict.REJECT,
            [f"The pull request is an exact duplicate of competing submission #{judgement.derived_from_pr}."],
            "duplicate_submission",
        )

    if files_changed == 0:
        return Decided(Verdict.REJECT, ["The pull request changes no files."], "empty_diff")

    findings = [_line(c) for c in judgement.checks]
    if judgement.derived_from_pr is not None:
        findings.append(f"Notice: This submission appears substantially derived from pull request #{judgement.derived_from_pr}.")
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

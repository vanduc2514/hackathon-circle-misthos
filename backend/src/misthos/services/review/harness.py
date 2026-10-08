"""Replay the review agent over a fixed corpus and report its agreement.

    python -m misthos.services.review.harness                 # the rule reviewer
    python -m misthos.services.review.harness --reviewer model # Claude, if configured
    python -m misthos.services.review.harness --corpus path.json --json

Each case is a pull request reduced to what a reviewer is given (the criteria, the
changed files, whether the checks pass, how many rework rounds came before) and the
verdict a person gave it. The report gives agreement overall and per complexity
band, because agreement on trivial patches is easy and hides failures in the
aggregate, and exits non-zero when any figure is below its floor. The CI suite runs
the same replay (tests/unit/test_review_agreement.py), so a regression in the
reviewer fails the build before it reaches a contributor.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from misthos.domain.money import Usdc
from misthos.domain.review import ChangedFile, Submitted, Verdict, decide
from misthos.services.review.base import Reviewer
from misthos.services.review.rules import RuleReviewer

CORPUS = Path(__file__).with_name("regression-corpus.json")

# 09 sets the guardrail at 85 percent agreement. A band may sit lower, but not by
# much: a reviewer that is only right on easy work is not trustworthy on hard work.
FLOORS = {"overall": 0.85, "low": 0.8, "medium": 0.8, "high": 0.8}


@dataclass(frozen=True)
class Case:
    id: str
    band: str
    expected: Verdict
    note: str
    submitted: Submitted
    rework_rounds: int


@dataclass
class Report:
    reviewer: str
    total: int = 0
    agreed: int = 0
    by_band: dict[str, list[int]] = field(default_factory=dict)
    """Band to [agreed, total]."""
    disagreements: list[dict[str, str]] = field(default_factory=list)
    cost_usdc: str = "0.000000"

    def rate(self, band: str | None = None) -> float:
        agreed, total = (self.agreed, self.total) if band is None else self.by_band[band]
        return agreed / total if total else 1.0

    def below_floor(self) -> list[str]:
        failing = []
        if self.rate() < FLOORS["overall"]:
            failing.append(f"overall {self.rate():.0%} < {FLOORS['overall']:.0%}")
        for band in sorted(self.by_band):
            floor = FLOORS.get(band, FLOORS["overall"])
            if self.rate(band) < floor:
                failing.append(f"{band} {self.rate(band):.0%} < {floor:.0%}")
        return failing


def load(path: Path = CORPUS) -> list[Case]:
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return [
        Case(
            id=c["id"],
            band=c["band"],
            expected=Verdict(c["expected"]),
            note=c.get("note", ""),
            rework_rounds=int(c.get("rework_rounds", 0)),
            submitted=Submitted(
                repo=c.get("repo", "corpus/case"),
                pr_number=int(c.get("pr_number", 1)),
                head_sha=c.get("head_sha", "0" * 40),
                title=c.get("title", c["id"]),
                criteria=tuple(c["criteria"]),
                files=tuple(
                    ChangedFile(
                        path=f["path"],
                        additions=int(f.get("additions", 0)),
                        deletions=int(f.get("deletions", 0)),
                        patch=f.get("patch", ""),
                    )
                    for f in c["files"]
                ),
                # null: the checks never reported, as in a repository with no CI.
                checks_passed=None if c["checks_passed"] is None else bool(c["checks_passed"]),
            ),
        )
        for c in raw["cases"]
    ]


def replay(reviewer: Reviewer, cases: list[Case]) -> Report:
    report = Report(reviewer=reviewer.name)
    spent = Usdc(0)
    for case in cases:
        judgement = reviewer.judge(case.submitted)
        spent = spent + judgement.cost
        verdict = decide(
            judgement,
            checks_passed=case.submitted.checks_passed,
            files_changed=len(case.submitted.files),
            rework_rounds=case.rework_rounds,
        ).verdict
        band = report.by_band.setdefault(case.band, [0, 0])
        band[1] += 1
        report.total += 1
        if verdict is case.expected:
            band[0] += 1
            report.agreed += 1
        else:
            report.disagreements.append(
                {"id": case.id, "expected": case.expected.value, "got": verdict.value,
                 "note": case.note}
            )  # fmt: skip
    report.cost_usdc = f"{spent.decimal:.6f}"
    return report


def render(report: Report) -> str:
    lines = [
        f"Review agreement for {report.reviewer}: {report.agreed}/{report.total} "
        f"({report.rate():.0%}), inference {report.cost_usdc} USDC",
        "",
        f"{'band':<8} {'agreed':>7} {'rate':>6} {'floor':>6}",
    ]
    for band in sorted(report.by_band):
        agreed, total = report.by_band[band]
        floor = FLOORS.get(band, FLOORS["overall"])
        lines.append(f"{band:<8} {agreed:>3}/{total:<3} {report.rate(band):>6.0%} {floor:>6.0%}")
    if report.disagreements:
        lines += ["", "Disagreements:"]
        lines += [
            f"  {d['id']}: expected {d['expected']}, got {d['got']} ({d['note']})"
            for d in report.disagreements
        ]
    failing = report.below_floor()
    lines += ["", "BELOW FLOOR: " + "; ".join(failing) if failing else "All floors met."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--reviewer", choices=["rules", "model"], default="rules")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    if args.reviewer == "model":
        from misthos.store import store

        reviewer = store.reviewer
        if isinstance(reviewer, RuleReviewer):
            print("No model is configured: set MISTHOS_ANTHROPIC_API_KEY.", file=sys.stderr)
            return 2
    else:
        reviewer = RuleReviewer()

    report = replay(reviewer, load(args.corpus))
    print(json.dumps(asdict(report), indent=2) if args.json else render(report))
    return 1 if report.below_floor() else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Calibrate the pricing engine against what work was actually worth (#41).

    python -m misthos.services.calibration                 # the historical corpus
    python -m misthos.services.calibration --settled       # the platform's settlements
    python -m misthos.services.calibration --write out.json

The engine's weights and rate are a starting guess. This replays it over issues whose
worth is known, from what a buyer paid or valued the work at, and reports how often
that worth falls inside the band and how far the recommendation was from it. It then
fits the rate, and with at least twenty issues the weights too, and prints what to
change. The change is a reviewed commit to `domain/pricing.py`, with the report
beside it, never something the engine does to itself.

The corpus is `calibration-corpus.json` beside this file: real issues only, each with
where its worth came from. An empty corpus is reported as empty, not filled in.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from statistics import median

from misthos.config import settings
from misthos.domain.issue import IssueState
from misthos.domain.money import Usdc
from misthos.domain.pricing import RATE_PER_HOUR, WEIGHTS, ComplexitySignals, propose

CORPUS = Path(__file__).with_name("calibration-corpus.json")

# Twenty is the plan's number (10, "Pricing engine on 20 historical issues"). Six
# weights fitted to fewer points would fit the noise.
MIN_FOR_WEIGHTS = 20
MIN_FOR_RATE = 5

STEP = 0.05
FLOOR = 0.05
MAX_ROUNDS = 200


@dataclass(frozen=True)
class Observation:
    id: str
    signals: dict[str, float]
    worth: Usdc
    source: str
    compliance_driven: bool = False


@dataclass
class Fit:
    weights: dict[str, float]
    rate: Usdc
    within_band: float
    median_error: float
    """Median absolute error of the recommendation, as a share of the worth."""
    log_rms: float


@dataclass
class Report:
    issues: int
    sources: list[str]
    current: Fit | None
    proposed: Fit | None
    notes: list[str] = field(default_factory=list)


def _price(obs: Observation, weights: dict[str, float], rate: Usdc):  # type: ignore[no-untyped-def]
    return propose(
        ComplexitySignals(**obs.signals),
        rate_per_hour=rate,
        compliance_driven=obs.compliance_driven,
        weights=weights,
    )


def evaluate(observations: list[Observation], weights: dict[str, float], rate: Usdc) -> Fit:
    inside, errors, logs = 0, [], []
    for obs in observations:
        p = _price(obs, weights, rate)
        worth = obs.worth.base_units
        inside += p.band_low.base_units <= worth <= p.band_high.base_units
        errors.append(abs(p.recommended.base_units - worth) / worth)
        logs.append(math.log(p.recommended.base_units / worth) ** 2)
    n = len(observations)
    return Fit(
        weights=dict(weights),
        rate=rate,
        within_band=round(inside / n, 3),
        median_error=round(median(errors), 3),
        log_rms=round(math.sqrt(sum(logs) / n), 4),
    )


def fit_rate(observations: list[Observation], weights: dict[str, float], rate: Usdc) -> Usdc:
    """The rate that centres the recommendations on the worth, geometrically: price is
    linear in the rate, so one ratio moves every recommendation by the same factor."""
    ratios = [
        math.log(obs.worth.base_units / _price(obs, weights, rate).recommended.base_units)
        for obs in observations
    ]
    factor = Decimal(str(math.exp(sum(ratios) / len(ratios))))
    return Usdc.from_decimal((rate.decimal * factor).quantize(Decimal("0.01")))


def fit_weights(observations: list[Observation], start: dict[str, float], rate: Usdc) -> Fit:
    """Move weight between signals a step at a time while it lowers the error, the
    rate refitted at every step. Deterministic, stays on the simplex, keeps every
    signal at a floor so none is dropped on a small sample."""
    weights = dict(start)
    best = evaluate(observations, weights, fit_rate(observations, weights, rate))
    for _ in range(MAX_ROUNDS):
        improved = False
        for give in weights:
            for take in weights:
                if give == take or weights[give] - STEP < FLOOR - 1e-9:
                    continue
                trial = dict(weights)
                trial[give] = round(trial[give] - STEP, 2)
                trial[take] = round(trial[take] + STEP, 2)
                fitted = evaluate(observations, trial, fit_rate(observations, trial, rate))
                if fitted.log_rms < best.log_rms - 1e-6:
                    weights, best, improved = trial, fitted, True
        if not improved:
            break
    return best


def calibrate(observations: list[Observation]) -> Report:
    sources = sorted({o.source for o in observations})
    if not observations:
        return Report(0, [], None, None, ["No issues with a known worth yet: nothing to fit."])
    current = evaluate(observations, WEIGHTS, RATE_PER_HOUR)
    notes: list[str] = []
    if len(observations) < MIN_FOR_RATE:
        notes.append(f"{len(observations)} issues: too few to fit even the rate.")
        return Report(len(observations), sources, current, None, notes)
    if len(observations) < MIN_FOR_WEIGHTS:
        rate = fit_rate(observations, WEIGHTS, RATE_PER_HOUR)
        notes.append(
            f"{len(observations)} issues: the rate is fitted, the weights are not; "
            f"that needs {MIN_FOR_WEIGHTS}."
        )
        return Report(len(observations), sources, current, evaluate(observations, WEIGHTS, rate),
                      notes)  # fmt: skip
    proposed = fit_weights(observations, WEIGHTS, RATE_PER_HOUR)
    return Report(len(observations), sources, current, proposed, notes)


def load_corpus(path: Path = CORPUS) -> list[Observation]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["issues"] if isinstance(data, dict) else data
    return [
        Observation(
            id=row["id"],
            signals={k: float(v) for k, v in row["signals"].items()},
            worth=Usdc.from_decimal(str(row["worth_usdc"])),
            source=row["source"],
            compliance_driven=bool(row.get("compliance_driven", False)),
        )
        for row in rows
    ]


def load_settled() -> list[Observation]:
    """What the platform itself settled: the price a publisher approved and paid."""
    from misthos.store import store

    observations = []
    for rec in store.list_issues({IssueState.PAID}):
        if rec.proposal is None or rec.paid is None:
            continue
        observations.append(
            Observation(
                id=rec.id,
                signals=dict(rec.proposal.signals),
                worth=rec.paid,
                source="settled on the platform",
                compliance_driven=rec.compliance_driven,
            )
        )
    return observations


def render(report: Report) -> str:
    lines = [f"{report.issues} issues with a known worth"]
    if report.sources:
        lines.append("sources: " + "; ".join(report.sources))

    def row(name: str, fit: Fit) -> str:
        return (
            f"{name:<9} rate {fit.rate.decimal:>8.2f} /h   worth inside the band "
            f"{fit.within_band:>6.1%}   median error {fit.median_error:>6.1%}"
        )

    if report.current:
        lines.append(row("current", report.current))
    if report.proposed:
        lines.append(row("proposed", report.proposed))
        if report.proposed.weights != (report.current.weights if report.current else None):
            lines.append("weights  " + json.dumps(report.proposed.weights))
    lines.extend(report.notes)
    return "\n".join(lines)


def _jsonable(report: Report) -> dict:
    def fit(f: Fit | None) -> dict | None:
        if f is None:
            return None
        out = asdict(f)
        out["rate"] = f"{f.rate.decimal:.2f}"
        return out

    return {
        "issues": report.issues,
        "sources": report.sources,
        "current": fit(report.current),
        "proposed": fit(report.proposed),
        "notes": report.notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--settled", action="store_true", help="use the platform's settlements")
    parser.add_argument("--write", type=Path, help="record the report as JSON")
    args = parser.parse_args(argv)

    if args.settled:
        observations = load_settled()
        if settings.simulated and not settings.database_url:
            print("Warning: these are the simulation's seeded settlements, not evidence.")
    else:
        observations = load_corpus(args.corpus)
    report = calibrate(observations)
    print(render(report))
    if args.write:
        args.write.write_text(json.dumps(_jsonable(report), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

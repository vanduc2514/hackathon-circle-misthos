"""#41: the calibration harness fits what it is given, and refuses to over-fit.

The observations here are synthetic on purpose: they come from a known rate and known
weights, so the test can check that the fit recovers them. They calibrate nothing.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from misthos.domain.money import Usdc
from misthos.domain.pricing import RATE_PER_HOUR, WEIGHTS, ComplexitySignals, propose
from misthos.services import calibration
from misthos.services.calibration import (
    MIN_FOR_WEIGHTS,
    SETTLED,
    Observation,
    calibrate,
    load_corpus,
    main,
    render,
)


def synthetic(n: int, *, rate: str, weights: dict[str, float], seed: int = 7) -> list[Observation]:
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        signals = {name: float(rng.randint(1, 5)) for name in WEIGHTS}
        worth = propose(
            ComplexitySignals(**signals), rate_per_hour=Usdc.from_decimal(rate), weights=weights
        ).recommended
        rows.append(Observation(id=f"syn-{i}", signals=signals, worth=worth, source="synthetic"))
    return rows


def test_the_shipped_corpus_is_real_or_empty() -> None:
    """An invented row would calibrate the engine to the invention."""
    for obs in load_corpus():
        assert obs.source and obs.source != "synthetic"
        assert obs.id.startswith("https://")


def test_nothing_to_fit_is_said_plainly() -> None:
    report = calibrate([])
    assert report.proposed is None and "nothing to fit" in render(report)


def test_the_rate_is_recovered_when_only_the_rate_is_off() -> None:
    report = calibrate(synthetic(8, rate="120", weights=WEIGHTS))
    assert report.proposed is not None and report.current is not None
    assert abs(report.proposed.rate.decimal - Decimal(120)) < Decimal(1)
    assert report.proposed.weights == WEIGHTS  # too few issues to touch the weights
    assert report.proposed.median_error < report.current.median_error


def test_weights_move_only_with_enough_issues_and_only_if_it_helps() -> None:
    skewed = {**WEIGHTS, "code_surface": 0.40, "blast_radius": 0.05, "test_coverage": 0.05}
    assert abs(sum(skewed.values()) - 1) < 1e-9
    report = calibrate(synthetic(MIN_FOR_WEIGHTS + 4, rate="90", weights=skewed))
    assert report.proposed is not None and report.current is not None
    assert report.proposed.weights != WEIGHTS
    assert abs(sum(report.proposed.weights.values()) - 1) < 1e-9
    assert min(report.proposed.weights.values()) >= 0.05 - 1e-9
    assert report.proposed.log_rms < report.current.log_rms
    assert report.proposed.weights["code_surface"] > WEIGHTS["code_surface"]


def test_the_report_is_recorded(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.json"
    rows = [
        {"id": f"https://github.com/acme/x/issues/{o.id}", "signals": o.signals,
         "worth_usdc": str(o.worth.decimal), "source": "test"}
        for o in synthetic(6, rate="100", weights=WEIGHTS)
    ]  # fmt: skip
    corpus.write_text(json.dumps({"issues": rows}))
    out = tmp_path / "report.json"
    assert main(["--corpus", str(corpus), "--write", str(out)]) == 0
    recorded = json.loads(out.read_text())
    assert recorded["issues"] == 6
    assert recorded["current"]["rate"] == f"{RATE_PER_HOUR.decimal:.2f}"
    assert recorded["proposed"]["rate"].startswith("100")


def as_settled(observations: list[Observation]) -> list[Observation]:
    return [replace(o, source=SETTLED) for o in observations]


def test_settlements_measure_self_consistency_and_fit_nothing() -> None:
    """A settled price is the price the engine recommended and the publisher approved,
    so the engine agrees with it by construction. Fitted to its own output, the
    weights would fit only the budget ceiling and the relist uplift."""
    skewed = {**WEIGHTS, "code_surface": 0.40, "blast_radius": 0.05, "test_coverage": 0.05}
    report = calibrate(as_settled(synthetic(MIN_FOR_WEIGHTS + 4, rate="90", weights=skewed)))
    assert report.current is not None
    assert report.proposed is None
    text = render(report)
    assert "the engine's own output" in text
    assert "self-consistency, not calibration" in text
    assert "nothing is fitted" in text.lower()


def test_one_settlement_among_scored_issues_still_fits_nothing() -> None:
    rows = synthetic(MIN_FOR_WEIGHTS + 4, rate="90", weights=WEIGHTS)
    report = calibrate(rows[:-1] + as_settled(rows[-1:]))
    assert report.proposed is None


def test_the_settled_report_says_so_when_printed_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        calibration, "load_settled", lambda: as_settled(synthetic(6, rate="100", weights=WEIGHTS))
    )
    out = tmp_path / "report.json"
    assert main(["--settled", "--write", str(out)]) == 0
    assert "self-consistency, not calibration" in capsys.readouterr().out
    recorded = json.loads(out.read_text())
    assert recorded["self_consistency"] is True and recorded["proposed"] is None

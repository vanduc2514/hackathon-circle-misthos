"""The review agent's agreement on the regression corpus, enforced in CI (#37).

The floors are the ones the harness reports against. A change to the reviewer, the
verdict rules or the corpus that drops any band below its floor fails the build,
and so does any change to which cases disagree: the three known blind spots are
the rule reviewer's documented limit, and a new disagreement is a regression even
while the rate still clears the floor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from misthos.services.review.harness import FLOORS, load, main, render, replay
from misthos.services.review.rules import RuleReviewer

KNOWN_BLIND_SPOTS = {"blind-wrong-default", "blind-core-import", "blind-half-up"}


def test_every_band_clears_its_floor() -> None:
    report = replay(RuleReviewer(), load())
    assert report.below_floor() == [], render(report)
    assert set(report.by_band) == {"low", "medium", "high"}
    assert report.rate() >= FLOORS["overall"]


def test_only_the_known_blind_spots_disagree() -> None:
    report = replay(RuleReviewer(), load())
    assert {d["id"] for d in report.disagreements} == KNOWN_BLIND_SPOTS, render(report)


def test_the_corpus_covers_every_verdict_in_every_band() -> None:
    cases = load()
    for band in ("low", "medium", "high"):
        assert {c.expected.value for c in cases if c.band == band} >= {"accept", "rework"}
    assert {c.expected.value for c in cases} == {"accept", "rework", "reject"}


def test_the_command_exits_non_zero_below_the_floor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        '{"cases": [{"id": "x", "band": "low", "expected": "reject", "criteria": ["A test."],'
        ' "checks_passed": true, "files": [{"path": "tests/test_a.py"}]}]}'
    )
    assert main(["--corpus", str(corpus)]) == 1
    assert "BELOW FLOOR" in capsys.readouterr().out
    assert main([]) == 0

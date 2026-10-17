"""The historical review corpus (#36), and the ratchet that guards it in CI (#37)."""

from __future__ import annotations

import json
from collections import Counter

import httpx
import pytest

from misthos.services.review.harness import (
    BASELINE,
    HISTORICAL,
    baseline_regressions,
    load,
    main,
    replay,
)
from misthos.services.review.history import (
    GitHub,
    Source,
    band_for,
    checks_from,
    criteria_from,
    fetch,
    load_licence,
    load_sources,
)
from misthos.services.review.rules import RuleReviewer

CORPUS = json.loads(HISTORICAL.read_text(encoding="utf-8"))
DECIDED = {
    "accept": "merged",
    "reject": "closed without merging",
    "rework": "changes requested by ",
}


class TestTheCorpus:
    def test_every_case_is_a_real_pull_request_a_person_decided(self) -> None:
        for case in CORPUS["cases"]:
            source = case["source"]
            assert source["pull_request"].startswith("https://github.com/"), case["id"]
            assert source["decided_at"], case["id"]
            assert source["decided"].startswith(DECIDED[case["expected"]]), case["id"]
            assert case["note"].strip(), case["id"]

    def test_the_corpus_is_exactly_the_curated_sources(self) -> None:
        _, sources = load_sources()
        listed = {(s.pr, s.expected) for s in sources}
        fetched = {(f"{c['repo']}#{c['pr_number']}", c["expected"]) for c in CORPUS["cases"]}
        assert fetched == listed

    def test_every_repository_it_copies_text_from_has_its_licence_named(self) -> None:
        # The corpus carries other projects' diffs, so it says whose and on what terms,
        # and the built file says it the same way the curated sources do.
        licence = load_licence()
        assert CORPUS["licence"] == licence
        assert "MIT licence in the repository's LICENSE" in licence["note"]
        for case in CORPUS["cases"]:
            assert licence["upstream"][case["repo"]].strip(), case["id"]

    def test_a_source_from_a_repository_with_no_licence_named_is_refused(self, tmp_path) -> None:
        path = tmp_path / "sources.json"
        path.write_text(json.dumps({
            "description": "", "licence": {"upstream": {"o/r": "MIT"}},
            "sources": [{"pr": "o/r#1", "expected": "accept", "why": "merged"},
                        {"pr": "x/y#2", "expected": "accept", "why": "merged"}],
        }))  # fmt: skip
        with pytest.raises(ValueError, match="name the licence x/y"):
            load_sources(path)

    def test_it_holds_every_verdict_in_numbers_worth_measuring(self) -> None:
        counts = Counter(case["expected"] for case in CORPUS["cases"])
        assert counts == {"accept": 12, "rework": 12, "reject": 12}

    def test_it_replays_offline_and_the_same_way_twice(self) -> None:
        first = replay(RuleReviewer(), load(HISTORICAL))
        second = replay(RuleReviewer(), load(HISTORICAL))
        assert first.disagreements == second.disagreements
        assert first.total == len(CORPUS["cases"])


class TestTheRatchet:
    def test_the_rule_reviewer_keeps_every_case_it_agreed_on(self) -> None:
        """No reviewer meets the floors on real pull requests yet, so CI holds the line
        instead: losing a case the baseline agreed on fails the build."""
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        report = replay(RuleReviewer(), load(HISTORICAL))
        assert baseline_regressions(report, baseline) == []

    def test_losing_an_agreed_case_is_a_regression(self) -> None:
        report = replay(RuleReviewer(), load(HISTORICAL))
        lost = report.disagreements[0]["id"]
        baseline = {"agreed": [lost], "rate": 0.0}
        assert baseline_regressions(report, baseline) == [f"no longer agrees on {lost}"]

    def test_a_baseline_judges_only_the_reviewer_it_was_recorded_for(self) -> None:
        # Claude measured against the rule reviewer's baseline would show the gap between
        # two reviewers as a regression.
        report = replay(RuleReviewer(), load(HISTORICAL))
        baseline = {"reviewer": "claude-x", "agreed": [], "rate": 0.0}
        (problem,) = baseline_regressions(report, baseline)
        assert "recorded for claude-x, not rules_v1" in problem

    def test_the_baseline_is_for_the_historical_corpus_only(self) -> None:
        for flag in ("--against-baseline", "--record-baseline"):
            with pytest.raises(SystemExit) as refused:
                main(["--corpus", "regression", flag])
            assert refused.value.code == 2

    def test_the_command_reads_the_corpus_by_name(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--corpus", "historical", "--against-baseline"]) == 0
        assert "Baseline held." in capsys.readouterr().out
        # Against the floors, honestly, the rule reviewer does not pass on history.
        assert main(["--corpus", "historical"]) == 1


class TestBuilding:
    def test_bands_follow_the_size_of_the_change(self) -> None:
        assert (band_for(10), band_for(100), band_for(1000)) == ("low", "medium", "high")

    def test_a_checklist_in_the_issue_becomes_the_criteria(self) -> None:
        body = "Steps:\n- [ ] Add a test for `-q`\n- [x] Document the flag\nThanks"
        assert criteria_from("Quiet mode", body) == ["Add a test for `-q`", "Document the flag"]
        assert criteria_from("Quiet mode", "no list here") == ["Quiet mode"]

    def test_checks_are_unknown_until_one_reports(self) -> None:
        assert checks_from([], []) is None
        assert checks_from([{"status": "completed", "conclusion": "success"}], []) is True
        assert checks_from([], [{"state": "failure"}]) is False

    def test_a_source_without_a_reason_is_refused(self, tmp_path) -> None:
        path = tmp_path / "sources.json"
        path.write_text(json.dumps({"description": "", "sources": [
            {"pr": "o/r#1", "expected": "accept", "why": " "}
        ]}))  # fmt: skip
        with pytest.raises(ValueError, match="decision a person made"):
            load_sources(path)


def _github(routes: dict[str, object]) -> GitHub:
    def answer(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=body)

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(answer))
    return GitHub(None, client)


PR = {
    "html_url": "https://github.com/o/r/pull/7",
    "title": "Fix the thing",
    "body": "Fixes #3",
    "merged_at": "2025-01-02T00:00:00Z",
    "closed_at": "2025-01-02T00:00:00Z",
    "head": {"sha": "f" * 40},
    "base": {"sha": "b" * 40},
}


class TestFetching:
    def test_a_case_listed_as_accepted_must_have_merged(self) -> None:
        gh = _github({"/repos/o/r/pulls/7": {**PR, "merged_at": None}})
        with pytest.raises(ValueError, match="never merged"):
            fetch(gh, Source("o/r#7", "accept", "merged"))

    def test_a_rework_case_is_the_pull_request_as_the_reviewer_saw_it(self) -> None:
        reviews = [
            {"id": 1, "state": "CHANGES_REQUESTED", "commit_id": "a" * 40,
             "submitted_at": "2025-01-01T00:00:00Z", "user": {"login": "early"}},
            {"id": 2, "state": "CHANGES_REQUESTED", "commit_id": "c" * 40,
             "submitted_at": "2025-01-01T12:00:00Z", "user": {"login": "maintainer"}},
        ]  # fmt: skip
        gh = _github(
            {
                "/repos/o/r/pulls/7": PR,
                "/repos/o/r/pulls/7/reviews": reviews,
                f"/repos/o/r/compare/{'b' * 40}...{'c' * 40}": {
                    "files": [{"filename": "src/a.py", "additions": 3, "deletions": 1}]
                },
                "/repos/o/r/issues/3": {
                    "title": "The thing is broken",
                    "body": "",
                    "html_url": "https://github.com/o/r/issues/3",
                },
            }
        )
        case = fetch(gh, Source("o/r#7", "rework", "asked for a test", at_review=2))

        assert case["head_sha"] == "c" * 40
        assert [f["path"] for f in case["files"]] == ["src/a.py"]
        assert case["rework_rounds"] == 1  # one request came before this one
        assert case["criteria"] == ["The thing is broken"]
        assert case["source"]["decided"] == "changes requested by maintainer"
        assert case["checks_passed"] is None  # the commit's checks could not be read

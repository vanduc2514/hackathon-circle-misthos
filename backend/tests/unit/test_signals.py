"""The six complexity signals, read from an issue and its repository's file list."""

from __future__ import annotations

from dataclasses import replace

import pytest

from misthos.domain.pricing import propose
from misthos.domain.signals import IssueFacts, TreeCounts, classify_tree, read

TREE = [
    "README.md",
    "pyproject.toml",
    ".github/workflows/ci.yml",
    "src/pkg/__init__.py",
    "src/pkg/core.py",
    "src/pkg/parse.py",
    "src/pkg/locale.py",
    "tests/test_core.py",
    "tests/test_parse.py",
    "web/package.json",
    "web/src/app.ts",
    "web/src/app.test.ts",
    "web/node_modules/left-pad/index.js",
    "web/node_modules/left-pad/package.json",
    "dist/bundle.js",
    "docs/guide.md",
]

CLEAR_BODY = """Parsing `de_CH` falls back to `de` instead of `de-CH`. Every Swiss customer
sees German-German formats for dates and currency since the 2.3 release.

Steps to reproduce:

```python
parse_locale("de_CH")
```

Expected: `de-CH`. Actual: `de`.

- [ ] add a test for underscore separators
"""


def facts(**changes: object) -> IssueFacts:
    base = IssueFacts(
        repo="globex/parse-locale",
        number=87,
        title="Underscore locales lose their region",
        body=CLEAR_BODY,
        labels=("bug",),
        tree=TreeCounts(source_files=120, test_files=40, has_ci=True, dependency_manifests=1),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


class TestTree:
    def test_counts_source_tests_manifests_and_ci(self) -> None:
        counts = classify_tree(TREE)
        assert counts == TreeCounts(
            source_files=5, test_files=3, has_ci=True, dependency_manifests=2
        )

    def test_vendored_and_built_code_is_not_the_projects(self) -> None:
        assert classify_tree(["node_modules/x/index.js", "dist/app.js"]).source_files == 0

    @pytest.mark.parametrize(
        "path",
        ["tests/unit/test_x.py", "pkg/x_test.go", "src/x.spec.ts", "src/FooTest.java"],
    )
    def test_recognises_tests_by_place_and_by_name(self, path: str) -> None:
        assert classify_tree([path]).test_files == 1

    def test_no_ci_file_means_no_ci(self) -> None:
        assert classify_tree(["src/a.py", ".github/dependabot.yml"]).has_ci is False


class TestSignals:
    def test_every_signal_is_scored_with_a_reason(self) -> None:
        reading = read(facts())
        assert set(reading.reasons) == set(reading.signals.as_dict())
        assert all(1 <= v <= 5 for v in reading.signals.as_dict().values())
        assert "test files" in reading.summary()

    def test_a_clear_issue_scores_low_on_clarity(self) -> None:
        assert read(facts()).signals.requirement_clarity == 1.0
        assert read(facts(body="it is broken")).signals.requirement_clarity == 5.0

    def test_named_files_decide_the_code_surface(self) -> None:
        one = read(facts(body="The bug is in `src/pkg/parse.py`."))
        many = read(facts(body=" ".join(f"src/m{i}.py" for i in range(9))))
        assert one.signals.code_surface == 1.0
        assert many.signals.code_surface == 5.0

    def test_without_named_files_the_repository_size_decides(self) -> None:
        small = read(facts(body="", tree=TreeCounts(source_files=10)))
        large = read(facts(body="", tree=TreeCounts(source_files=5000)))
        assert small.signals.code_surface < large.signals.code_surface

    def test_a_well_tested_repository_needs_less_work(self) -> None:
        tested = read(facts(tree=TreeCounts(source_files=100, test_files=60, has_ci=True)))
        bare = read(facts(tree=TreeCounts(source_files=100, test_files=0, has_ci=True)))
        assert tested.signals.test_coverage == 1.0
        assert bare.signals.test_coverage == 5.0

    def test_tests_nobody_runs_count_for_less(self) -> None:
        with_ci = read(facts(tree=TreeCounts(source_files=100, test_files=20, has_ci=True)))
        without = read(facts(tree=TreeCounts(source_files=100, test_files=20, has_ci=False)))
        assert without.signals.test_coverage == with_ci.signals.test_coverage + 1
        assert "no CI" in without.reasons["test_coverage"]

    def test_failed_attempts_raise_the_score(self) -> None:
        scores = [read(facts(prior_attempts=n)).signals.prior_attempts for n in range(6)]
        assert scores == [1.0, 2.0, 3.0, 4.0, 5.0, 5.0]

    @pytest.mark.parametrize(
        ("labels", "score"),
        [(("security",), 5.0), (("regression",), 4.0), (("bug",), 3.0),
         (("enhancement",), 2.0), (("documentation",), 1.0), ((), 2.0)],
    )  # fmt: skip
    def test_labels_set_the_blast_radius(self, labels: tuple[str, ...], score: float) -> None:
        assert read(facts(labels=labels)).signals.blast_radius == score

    def test_the_price_moves_when_the_issue_does(self) -> None:
        """The read path's done-when: a changed issue is a changed proposal."""
        before = propose(read(facts()).signals)
        after = propose(read(facts(body="it is broken", labels=("security",))).signals)
        assert after.recommended > before.recommended

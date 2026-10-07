"""The six complexity signals, read from the issue and its repository.

Until now the pricing engine scored signals a fixture declared. This module turns
what the GitHub read path learns into those scores: how much code the fix is likely
to touch, how clearly the issue says what done means, how well the repository tests
itself, how many dependency manifests it carries, how often someone already tried
and failed, and how much breaks if the fix is wrong.

Every rule is a first approximation, written so it can be read and argued with, and
calibrated against settled prices in #11. Each score comes with the reason for it,
which goes into the decision log next to the price. Nothing here touches the network.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from misthos.domain.pricing import ComplexitySignals

SOURCE_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs",
        ".java", ".kt", ".scala", ".rb", ".php", ".cs", ".c", ".h", ".cc", ".cpp",
        ".hpp", ".swift", ".m", ".sol", ".ex", ".exs", ".erl", ".clj", ".dart",
        ".lua", ".r", ".jl", ".zig", ".vue", ".svelte",
    }
)  # fmt: skip

MANIFESTS = frozenset(
    {
        "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
        "pipfile", "go.mod", "cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
        "gemfile", "composer.json", "mix.exs", "pubspec.yaml", "foundry.toml",
        "package.swift", "project.clj", "deno.json",
    }
)  # fmt: skip

# Directories that hold someone else's code or build output, not the project's.
VENDORED = frozenset(
    {"node_modules", "vendor", "third_party", "dist", "build", ".venv", "venv", "target"}
)

_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing"})
_TEST_NAME = re.compile(
    r"(^test_.*|.*_test\.\w+$|.*\.(test|spec)\.\w+$|.*Tests?\.\w+$)", re.IGNORECASE
)
_CI_FILES = (
    re.compile(r"^\.github/workflows/[^/]+\.ya?ml$"),
    re.compile(r"^\.gitlab-ci\.ya?ml$"),
    re.compile(r"^\.circleci/config\.ya?ml$"),
    re.compile(r"^azure-pipelines\.ya?ml$"),
    re.compile(r"^jenkinsfile$", re.IGNORECASE),
)


@dataclass(frozen=True)
class TreeCounts:
    source_files: int = 0
    test_files: int = 0
    has_ci: bool = False
    dependency_manifests: int = 0


def _vendored(path: PurePosixPath) -> bool:
    return any(p.lower() in VENDORED for p in path.parts[:-1])


def is_test_path(raw: str) -> bool:
    """A source file that tests the project rather than being part of it."""
    path = PurePosixPath(raw)
    if path.suffix.lower() not in SOURCE_SUFFIXES or _vendored(path):
        return False
    parts = [p.lower() for p in path.parts[:-1]]
    return any(p in _TEST_DIRS for p in parts) or bool(_TEST_NAME.match(path.name))


def is_changelog_path(raw: str) -> bool:
    name = PurePosixPath(raw).name.lower()
    return name.startswith(("changelog", "changes", "history", "news")) or raw.lower().startswith(
        ("changelog.d/", "changes/", "newsfragments/")
    )


def is_docs_path(raw: str) -> bool:
    """Documentation or a readme. A changelog entry is not documentation."""
    if is_changelog_path(raw):
        return False
    path = PurePosixPath(raw)
    lowered = [p.lower() for p in path.parts]
    return (
        path.name.lower().startswith("readme")
        or any(p in {"docs", "doc", "documentation"} for p in lowered[:-1])
        or path.suffix.lower() in {".md", ".rst", ".adoc"}
    )


def classify_tree(paths: Iterable[str]) -> TreeCounts:
    """Count what a repository's file list says about it."""
    source = tests = manifests = 0
    ci = False
    for raw in paths:
        path = PurePosixPath(raw)
        if _vendored(path):
            continue
        if any(rule.match(raw) for rule in _CI_FILES):
            ci = True
        if path.name.lower() in MANIFESTS:
            manifests += 1
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        if is_test_path(raw):
            tests += 1
        else:
            source += 1
    return TreeCounts(
        source_files=source, test_files=tests, has_ci=ci, dependency_manifests=manifests
    )


@dataclass(frozen=True)
class IssueFacts:
    """What the read path learned about one issue and the repository it lives in."""

    repo: str
    number: int
    title: str
    body: str
    labels: tuple[str, ...] = ()
    tree: TreeCounts = field(default_factory=TreeCounts)
    prior_attempts: int = 0
    """Pull requests that referenced the issue and were closed without merging."""


@dataclass(frozen=True)
class Reading:
    signals: ComplexitySignals
    reasons: dict[str, str]
    """One sentence per signal saying why it scored what it did."""

    def summary(self) -> str:
        return "; ".join(
            f"{name.replace('_', ' ')} {score:g} ({self.reasons[name]})"
            for name, score in self.signals.as_dict().items()
        )


_PATH_MENTION = re.compile(r"(?<![\w/])((?:[\w.-]+/)*[\w-]+\.[A-Za-z]{1,6})\b")
_STRUCTURE = re.compile(
    r"steps to reproduce|expected|actual|acceptance|should|must|repro", re.IGNORECASE
)

_BLAST = (
    (5.0, ("security", "vulnerability", "cve", "data-loss", "data loss")),
    (4.0, ("breaking", "regression", "critical", "crash", "outage")),
    (3.0, ("bug", "defect")),
    (2.0, ("feature", "enhancement", "performance", "refactor")),
    (1.0, ("docs", "documentation", "typo", "chore", "good first issue")),
)


def _step(value: float, thresholds: tuple[float, ...]) -> float:
    """1 plus one for every threshold `value` reaches, so 1 to len(thresholds) + 1."""
    return 1.0 + sum(1 for t in thresholds if value >= t)


def _mentioned_files(body: str) -> set[str]:
    found = {m.group(1) for m in _PATH_MENTION.finditer(body)}
    return {f for f in found if PurePosixPath(f).suffix.lower() in SOURCE_SUFFIXES}


def read(facts: IssueFacts) -> Reading:
    """Score the six signals from what the read path learned. Higher means more work."""
    reasons: dict[str, str] = {}
    tree = facts.tree

    mentioned = _mentioned_files(facts.body)
    if mentioned:
        code_surface = _step(len(mentioned), (2, 3, 5, 8))
        reasons["code_surface"] = f"the issue names {len(mentioned)} source file(s)"
    else:
        code_surface = _step(tree.source_files, (30, 150, 600, 2500))
        reasons["code_surface"] = (
            f"no files named, so judged from the repository's {tree.source_files} source files"
        )

    body = facts.body.strip()
    clarity = 5.0
    kept: list[str] = []
    if len(body) >= 200:
        clarity -= 1
        kept.append("a full description")
    if "```" in body or "Traceback" in body:
        clarity -= 1
        kept.append("code or a trace")
    if _STRUCTURE.search(body):
        clarity -= 1
        kept.append("expected behaviour")
    if re.search(r"^\s*[-*] \[[ xX]\]", body, re.MULTILINE):
        clarity -= 1
        kept.append("a checklist")
    reasons["requirement_clarity"] = (
        "the issue has " + ", ".join(kept) if kept else "the issue says little about done"
    )

    if tree.source_files == 0:
        test_coverage = 3.0
        reasons["test_coverage"] = "no source files recognised, so coverage is unknown"
    else:
        ratio = tree.test_files / tree.source_files
        test_coverage = 6.0 - _step(ratio, (0.000001, 0.1, 0.25, 0.5))
        reasons["test_coverage"] = (
            f"{tree.test_files} test files for {tree.source_files} source files"
        )
    if not tree.has_ci:
        test_coverage = min(5.0, test_coverage + 1)
        reasons["test_coverage"] += ", and no CI runs them"

    dependency_depth = _step(tree.dependency_manifests, (1, 2, 4, 10))
    reasons["dependency_depth"] = f"{tree.dependency_manifests} dependency manifest(s)"

    prior_attempts = _step(facts.prior_attempts, (1, 2, 3, 4))
    reasons["prior_attempts"] = (
        f"{facts.prior_attempts} earlier pull request(s) closed without merging"
    )

    labels = [label.lower() for label in facts.labels]
    blast_radius, why = 2.0, "no label says otherwise"
    for score, words in _BLAST:
        hit = next((label for label in labels if any(w in label for w in words)), None)
        if hit is not None:
            blast_radius, why = score, f"labelled {hit}"
            break
    reasons["blast_radius"] = why

    signals = ComplexitySignals(
        code_surface=code_surface,
        requirement_clarity=max(1.0, clarity),
        test_coverage=test_coverage,
        dependency_depth=dependency_depth,
        prior_attempts=prior_attempts,
        blast_radius=blast_radius,
    )
    signals.validate()
    return Reading(signals=signals, reasons=reasons)

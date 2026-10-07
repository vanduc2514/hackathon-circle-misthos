"""Acceptance criteria a reviewer can check (#38).

The criteria are the standard every verdict is measured against, so a vague one makes
every verdict arguable. This module does two things with them:

- `draft` writes the first version from the issue itself: what kind of change it is,
  the files it names, the publisher's own task list. Every drafted criterion names
  something a reviewer can look at: a test, a file, a document, an output, a number.
- `problems` refuses what cannot be judged, before the publisher can approve it: a
  question, a fragment, a judgement word with nothing to check it against ("works
  correctly"), several criteria run together, or a repeat. It is tuned to refuse the
  clearly uncheckable rather than to second-guess specific prose: "Backoff includes
  jitter by default" passes, "Loader is pluggable" does not.

Pure functions, like the rest of the domain.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from misthos.domain.signals import mentioned_files

MAX_CRITERIA = 12
MIN_WORDS = 4
MAX_LENGTH = 300
MAX_ANDS = 2
SHOWN_FILES = 3

# Words that judge without saying how a reviewer would see it.
_VAGUE = re.compile(
    r"\b(properly|correctly|correct|works?|working|better|improved?|improves|improvement|"
    r"cleaner|cleanly|nicer|nicely|nice|good|best|robust|user[- ]friendly|intuitive|"
    r"fast|faster|quick|quicker|quickly|efficient|efficiently|appropriate|appropriately|"
    r"reasonable|reasonably|seamless|seamlessly|optimi[sz]ed?|easy|easier|simple|simpler|"
    r"readable|maintainable|high[- ]quality|ok|okay|as expected|etc|and so on)\b",
    re.IGNORECASE,
)

# What a reviewer can look at. One of these next to a judgement word anchors it.
_ANCHOR = re.compile(
    r"(`[^`]+`|\"[^\"]+\"|\b\d+(?:\.\d+)?\b|\b[\w-]+(?:/[\w.-]+)*\.[a-z]{1,6}\b|"
    r"\b(?:tests?|tested|fuzz|benchmark|ci|checks?|lint|build|command|cli|flag|option|"
    r"endpoint|api|returns?|status|exit code|errors?|exception|message|logs?|output|"
    r"prints?|response|header|field|schema|migration|readme|docs?|documented|"
    r"documentation|changelog|example|signature|configs?|configuration|default|"
    r"version|input)\b)",
    re.IGNORECASE,
)

_TASK = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Problem:
    index: int
    """1-based, in the order the criteria were given."""
    criterion: str
    reason: str


def problems(criteria: Sequence[str]) -> list[Problem]:
    """What a reviewer could not judge, criterion by criterion. Empty when all can be."""
    found: list[Problem] = []
    if len(criteria) > MAX_CRITERIA:
        found.append(
            Problem(MAX_CRITERIA + 1, criteria[MAX_CRITERIA], f"more than {MAX_CRITERIA} "
                    "criteria is a project, not an issue; split it")
        )  # fmt: skip
    seen: dict[str, int] = {}
    for index, raw in enumerate(criteria[:MAX_CRITERIA], start=1):
        text = " ".join(raw.split())
        reason = _problem(text)
        key = text.lower().rstrip(".")
        if reason is None and key in seen:
            reason = f"repeats criterion {seen[key]}"
        seen.setdefault(key, index)
        if reason:
            found.append(Problem(index, text, reason))
    return found


def _problem(text: str) -> str | None:
    if not text:
        return "is empty"
    if text.rstrip().endswith("?"):
        return "is a question; say what must be true instead"
    if len(text) > MAX_LENGTH:
        return f"is longer than {MAX_LENGTH} characters; split it"
    if len(re.findall(r"\band\b", text, re.IGNORECASE)) > MAX_ANDS:
        return "asks for several things at once; make each its own criterion"
    vague = _VAGUE.search(text)
    if vague and not _ANCHOR.search(text):
        return (
            f'says "{vague.group(0)}" without saying how a reviewer would see it: name a '
            "test, a file, an output or a number"
        )
    if len(re.findall(r"[\w'-]+", text)) < MIN_WORDS:
        return "is too short to judge; say what a reviewer would look for"
    return None


def _kind(title: str, labels: Iterable[str]) -> str:
    tags = {label.lower() for label in labels}
    lowered = title.lower()
    if tags & {"dependencies", "dependency"} or re.match(
        r"(upgrade|bump|update|pin)\b.*\b(dependency|dependencies|version|package|library|parser)\b",
        lowered,
    ):
        return "dependency"
    if tags & {"security", "cve", "vulnerability"} or re.search(
        r"\b(security|vulnerab\w*|cve|overflow|out-of-bounds|injection|xss|memory safety)\b",
        lowered,
    ):
        return "security"
    if tags & {"docs", "documentation"}:
        return "docs"
    if tags & {"performance", "perf"} or re.search(r"\b(slow|performance|latency)\b", lowered):
        return "performance"
    if tags & {"feature", "enhancement", "feature-request"} or re.match(
        r"(add|support|allow|implement|introduce|expose)\b", lowered
    ):
        return "feature"
    return "bug"


def draft(
    title: str,
    body: str = "",
    labels: Iterable[str] = (),
) -> list[str]:
    """The first version of the criteria, from the issue itself, every one checkable.

    The project's own checks are not a criterion: the verdict already requires them.
    """
    labels = list(labels)
    subject = " ".join(title.split())
    if len(subject) > 80:
        subject = subject[:77].rstrip() + "..."
    kind = _kind(subject, labels)
    files = sorted(mentioned_files(body or ""))[:SHOWN_FILES]
    where = ", ".join(f"`{f}`" for f in files)

    drafted: list[str] = []
    # The publisher's own task list comes first, where a reviewer can judge it.
    tasks = [" ".join(t.split()) for t in _TASK.findall(body or "")]
    drafted.extend(t if t.endswith(".") else t + "." for t in tasks if _problem(t) is None)

    if kind == "dependency":
        drafted.append(f'The manifest and the lock file pin a version that resolves "{subject}".')
        drafted.append("The project's CI checks pass on the new version.")
    elif kind == "docs":
        drafted.append(f'The README or the docs are updated to cover "{subject}".')
        drafted.append("Every code example the change adds runs as written.")
    elif kind == "security":
        drafted.append(
            f'A regression test feeds the input behind "{subject}" and fails without the fix.'
        )
        drafted.append(
            "With the fix, that input is refused with an error, never a crash or a hang."
        )
    elif kind == "performance":
        drafted.append(
            f'A benchmark or test measures "{subject}" before and after the change, and the '
            "pull request reports both numbers."
        )
    elif kind == "feature":
        drafted.append(f'Tests cover the new behaviour, "{subject}", including an edge case.')
        drafted.append("The README or the docs describe the new behaviour with an example.")
    else:
        drafted.append(
            f'A test reproduces "{subject}": it fails without the fix and passes with it.'
        )
    if files:
        drafted.append(f"The change is made in {where}, which the issue names.")
    if kind == "security" or (kind == "dependency" and re.search(r"\bcve\b|secur", subject, re.I)):
        drafted.append("The changelog entry names the problem and the versions it affects.")
    elif kind != "docs":
        drafted.append("The changelog records the change.")

    unique: list[str] = []
    for criterion in drafted:
        if criterion.lower() not in {u.lower() for u in unique}:
            unique.append(criterion)
    return unique[:MAX_CRITERIA]

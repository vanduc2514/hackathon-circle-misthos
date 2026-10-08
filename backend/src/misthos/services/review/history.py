"""Build the historical review corpus from pull requests a person already decided (#36).

    GITHUB_TOKEN=... python -m misthos.services.review.history          # rebuild it
    python -m misthos.services.review.harness --corpus historical      # replay it

The regression corpus beside this file is constructed: it pins what the rule reviewer
must conclude. This one is history. Each case is a real public pull request, chosen
in `historical-sources.json` with the decision a maintainer made and where they made
it, and fetched here as a reviewer would be given it: the linked issue as the
criteria, the changed files with their patches, whether the checks passed, and how
many rounds of changes were requested first.

Building needs the network and is done once; the fetched corpus is committed, so the
replay is offline and reproducible, and a rebuild is a reviewed diff. Nothing here
invents a verdict: a source without a decision a person made is not a case.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).parent
SOURCES = HERE / "historical-sources.json"
CORPUS = HERE / "historical-corpus.json"

API = "https://api.github.com"
# What a reviewer is shown of each file. Enough to judge a fix; a vendored blob or a
# lockfile would otherwise dominate the corpus.
PATCH_CHARS = 4000
MAX_FILES = 60
MAX_CRITERIA = 12
CRITERION_CHARS = 300

# A pull request's size sets its band: agreement on a one-line fix says little about
# a two-hundred-line one, which is why the harness reports each band on its own.
LOW_LINES = 40
MEDIUM_LINES = 250

_CLOSES = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)")
_CHECKBOX = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Source:
    pr: str
    """owner/repo#number"""
    expected: str
    why: str
    at_review: int | None = None
    """For a rework case: the review that asked for changes. The case is the pull
    request as that reviewer saw it, at the commit they reviewed, not as it merged."""

    @property
    def repo(self) -> str:
        return self.pr.split("#")[0]

    @property
    def number(self) -> int:
        return int(self.pr.split("#")[1])


def band_for(lines_changed: int) -> str:
    if lines_changed < LOW_LINES:
        return "low"
    if lines_changed < MEDIUM_LINES:
        return "medium"
    return "high"


def criteria_from(issue_title: str, issue_body: str) -> list[str]:
    """The linked issue as the bar: its checklist if it has one, else its title.

    Historical issues were not written as Misthos criteria, so this is the closest
    honest reading of what the person who merged or declined the work asked for.
    """
    boxes = [m.group(1).strip() for m in _CHECKBOX.finditer(issue_body or "")]
    lines = boxes or [issue_title.strip()]
    return [line[:CRITERION_CHARS] for line in lines if line][:MAX_CRITERIA]


def checks_from(check_runs: list[dict], statuses: list[dict]) -> bool | None:
    """True when every check that reported passed, False on any failure, None if none."""
    outcomes = [r.get("conclusion") for r in check_runs if r.get("status") == "completed"]
    outcomes += [s.get("state") for s in statuses]
    if not outcomes:
        return None
    failed = {"failure", "timed_out", "cancelled", "action_required", "error"}
    return not any(o in failed for o in outcomes)


class GitHub:
    def __init__(self, token: str | None, client: httpx.Client | None = None) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = client or httpx.Client(base_url=API, headers=headers, timeout=30)

    def get(self, path: str, **params: Any) -> Any:
        response = self._http.get(path, params=params or None)
        response.raise_for_status()
        return response.json()

    def pages(self, path: str, limit: int) -> list[dict]:
        out: list[dict] = []
        page = 1
        while len(out) < limit:
            batch = self.get(path, per_page=100, page=page)
            out += batch
            if len(batch) < 100:  # a short page is the last one
                break
            page += 1
        return out[:limit]


def fetch(gh: GitHub, source: Source) -> dict[str, Any]:
    repo, number = source.repo, source.number
    pr = gh.get(f"/repos/{repo}/pulls/{number}")
    merged = bool(pr.get("merged_at"))
    if source.expected == "accept" and not merged:
        raise ValueError(f"{source.pr} is listed as accepted but was never merged")
    if source.expected == "reject" and merged:
        raise ValueError(f"{source.pr} is listed as rejected but was merged")

    reviews = gh.pages(f"/repos/{repo}/pulls/{number}/reviews", 300)
    if source.at_review is None:
        if source.expected == "rework":
            raise ValueError(f"{source.pr}: a rework case names the review that asked for it")
        files = gh.pages(f"/repos/{repo}/pulls/{number}/files", MAX_FILES)
        sha = pr["head"]["sha"]
        before = reviews
        decided_at = pr.get("merged_at") or pr.get("closed_at")
        decided = "merged" if merged else "closed without merging"
    else:
        review = next((r for r in reviews if r["id"] == source.at_review), None)
        if review is None or review.get("state") != "CHANGES_REQUESTED":
            raise ValueError(f"{source.pr}: review {source.at_review} did not request changes")
        sha = review["commit_id"]
        diff = gh.get(f"/repos/{repo}/compare/{pr['base']['sha']}...{sha}")
        files = diff.get("files", [])[:MAX_FILES]
        before = [r for r in reviews if r["submitted_at"] < review["submitted_at"]]
        decided_at = review["submitted_at"]
        decided = f"changes requested by {review['user']['login']}"
    try:
        runs = gh.get(f"/repos/{repo}/commits/{sha}/check-runs", per_page=100)["check_runs"]
        statuses = gh.get(f"/repos/{repo}/commits/{sha}/status").get("statuses", [])
    except httpx.HTTPStatusError:
        runs, statuses = [], []  # a deleted fork's head can no longer be read

    linked = [int(n) for n in _CLOSES.findall(pr.get("body") or "")]
    criteria: list[str] = []
    issue_ref = None
    for n in linked[:1]:
        try:
            issue = gh.get(f"/repos/{repo}/issues/{n}")
        except httpx.HTTPStatusError:
            continue
        criteria = criteria_from(issue.get("title", ""), issue.get("body") or "")
        issue_ref = issue.get("html_url")
    if not criteria:
        # No linked issue that could be read: the pull request's own title is what
        # the person deciding it was asked to accept.
        criteria = [pr["title"][:CRITERION_CHARS]]

    lines = sum(int(f.get("additions", 0)) + int(f.get("deletions", 0)) for f in files)
    return {
        "id": f"{repo.replace('/', '-')}-{number}"
        + (f"-review-{source.at_review}" if source.at_review else ""),
        "band": band_for(lines),
        "expected": source.expected,
        "note": source.why,
        "source": {
            "pull_request": pr["html_url"],
            "issue": issue_ref,
            "decided": decided,
            "decided_at": decided_at,
        },
        "repo": repo,
        "pr_number": number,
        "head_sha": sha,
        "title": pr["title"],
        "criteria": criteria,
        "checks_passed": checks_from(runs, statuses),
        "rework_rounds": sum(1 for r in before if r.get("state") == "CHANGES_REQUESTED"),
        "files": [
            {
                "path": f["filename"],
                "additions": int(f.get("additions", 0)),
                "deletions": int(f.get("deletions", 0)),
                "patch": (f.get("patch") or "")[:PATCH_CHARS],
            }
            for f in files
        ],
    }


def load_sources(path: Path = SOURCES) -> tuple[str, list[Source]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources = [Source(**s) for s in raw["sources"]]
    for s in sources:
        if s.expected not in {"accept", "rework", "reject"}:
            raise ValueError(f"{s.pr}: {s.expected} is not a verdict")
        if not s.why.strip():
            raise ValueError(f"{s.pr}: a case needs the decision a person made, and where")
    return raw["description"], sources


def build(gh: GitHub, sources_path: Path = SOURCES) -> dict[str, Any]:
    description, sources = load_sources(sources_path)
    return {"description": description, "cases": [fetch(gh, s) for s in sources]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sources", type=Path, default=SOURCES)
    parser.add_argument("--out", type=Path, default=CORPUS)
    args = parser.parse_args(argv)

    gh = GitHub(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    corpus = build(gh, args.sources)
    args.out.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    bands: dict[str, int] = {}
    for case in corpus["cases"]:
        bands[case["band"]] = bands.get(case["band"], 0) + 1
    print(f"wrote {len(corpus['cases'])} cases to {args.out}: {bands}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

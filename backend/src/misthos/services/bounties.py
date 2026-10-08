"""Build the calibration corpus from bounties that were actually paid (#41).

    GITHUB_TOKEN=... python -m misthos.services.bounties     # rebuild the corpus
    python -m misthos.services.calibration                   # calibrate against it

The engine prices a fixed fee for a GitHub issue before anyone works on it. The
closest public record of that is a bounty: a price someone put on an issue up front,
and paid when the work was accepted. Algora's bot leaves both on the issue, a
"💎 $N bounty" comment when it is priced and "🎉🎈 @who has been awarded **$N**" when
it is paid, so each case here is an issue with both, and its worth is what was paid,
with the URL of the award as the source.

The six signals are not scored by hand. Each issue is read the way the platform reads
one (title, body, labels, the repository's tree, the pull requests that tried and were
closed) and scored by `domain/signals.read`, the same rules that price a real issue,
so the calibration fits the engine to its own readings, not to someone's opinion. Two
caveats travel with every row: the tree is the repository as it is today, not as it
was when the bounty was posted, and the attempts include every one before the award.

Building needs the network; the corpus is committed, so calibration is offline.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from misthos.domain.signals import IssueFacts, classify_tree, read
from misthos.services.calibration import CORPUS
from misthos.services.github.app import abandoned_attempts

SOURCES = Path(__file__).with_name("calibration-sources.json")
API = "https://api.github.com"

_PRICED = re.compile(r"💎\s*\**\$([\d,]+(?:\.\d+)?)\**\s*bounty", re.IGNORECASE)
_AWARDED = re.compile(r"has been awarded \*\*\$([\d,]+(?:\.\d+)?)\*\*")


@dataclass(frozen=True)
class Paid:
    priced: Decimal
    awarded: Decimal
    award_url: str


def _amount(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def bounty_in(comments: list[dict[str, Any]]) -> Paid | None:
    """The price put on the issue up front, and what was paid for it.

    Only the bounty bot's own comments count, and only an award that announces a
    payment ("🎉🎈 ... has been awarded"), so a tip promised later or a claim that was
    never paid does not become a worth. A bounty split between people sums its awards.
    """
    bot = [c for c in comments if "algora" in (c.get("user") or {}).get("login", "").lower()]
    priced = next((m for c in bot if (m := _PRICED.search(c.get("body") or ""))), None)
    awards = [
        (c, m)
        for c in bot
        if (c.get("body") or "").lstrip().startswith("🎉")
        for m in [_AWARDED.search(c.get("body") or "")]
        if m
    ]
    if priced is None or not awards:
        return None
    return Paid(
        priced=_amount(priced.group(1)),
        awarded=sum((_amount(m.group(1)) for _, m in awards), Decimal(0)),
        award_url=awards[-1][0]["html_url"],
    )


class GitHub:
    def __init__(self, token: str | None, client: httpx.Client | None = None) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = client or httpx.Client(base_url=API, headers=headers, timeout=60)

    def get(self, path: str, **params: Any) -> Any:
        response = self._http.get(path, params=params or None)
        response.raise_for_status()
        return response.json()

    def pages(self, path: str, limit: int = 1000) -> list[dict]:
        out: list[dict] = []
        page = 1
        while len(out) < limit:
            batch = self.get(path, per_page=100, page=page)
            out += batch
            if len(batch) < 100:
                break
            page += 1
        return out


def facts_for(gh: GitHub, repo: str, number: int) -> IssueFacts:
    """The issue as the platform's GitHub read path sees it, from the public API."""
    issue = gh.get(f"/repos/{repo}/issues/{number}")
    meta = gh.get(f"/repos/{repo}")
    tree = gh.get(f"/repos/{repo}/git/trees/{meta['default_branch']}", recursive="1")
    paths = [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]
    timeline = gh.pages(f"/repos/{repo}/issues/{number}/timeline")
    return IssueFacts(
        repo=repo,
        number=number,
        title=issue["title"],
        body=issue.get("body") or "",
        labels=tuple(label["name"] for label in issue.get("labels", [])),
        tree=classify_tree(paths),
        prior_attempts=abandoned_attempts(timeline),
    )


def row_for(gh: GitHub, ref: str) -> dict[str, Any]:
    repo, number = ref.split("#")[0], int(ref.split("#")[1])
    paid = bounty_in(gh.pages(f"/repos/{repo}/issues/{number}/comments"))
    if paid is None:
        raise ValueError(f"{ref} has no bounty that was both priced up front and paid")
    reading = read(facts_for(gh, repo, number))
    return {
        "id": f"https://github.com/{repo}/issues/{number}",
        "signals": reading.signals.as_dict(),
        "reasons": reading.reasons,
        "worth_usdc": str(paid.awarded),
        "priced_usdc": str(paid.priced),
        "source": paid.award_url,
        "compliance_driven": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sources", type=Path, default=SOURCES)
    parser.add_argument("--out", type=Path, default=CORPUS)
    args = parser.parse_args(argv)

    sources = json.loads(args.sources.read_text(encoding="utf-8"))
    gh = GitHub(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    rows = [row_for(gh, ref) for ref in sources["issues"]]
    corpus = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else {}
    corpus["issues"] = rows
    args.out.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} paid bounties to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

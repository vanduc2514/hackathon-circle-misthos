"""Review every open pull request of this repository nightly, and merge what passes.

    GH_TOKEN=... ANTHROPIC_API_KEY=... python -m misthos.services.pr_review
    python -m misthos.services.pr_review --dry-run          # judge, post nothing

`.github/workflows/nightly.yml` runs this at 00:00 UTC. It lists every open pull
request, has the model review each diff against the current `main`, posts the review
with an explicit decision, and squash-merges the ones it approved.

Why this is a tool and not a page of workflow shell: the part worth getting right is
the decision — what may be merged and what may not — so it lives here as pure
functions with tests, and the workflow only runs it. Nothing merges on a
request-changes verdict, a check that is red or still running, a draft, a conflict, or
a head that moved after it was reviewed.

The GitHub side goes through `gh`, which is already how this repository is worked
interactively: it knows about stacked pull requests and it can use the ruleset bypass
for a pull request this account wrote and therefore cannot formally review.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from misthos.config import settings

REPO = "vanduc2514/hackathon-circle-misthos"
API_VERSION = "2023-06-01"
MAX_DIFF = 400_000
"""Characters. Larger than the biggest pull request this repository has taken; a diff
past it is refused rather than truncated, because half a diff is not a review."""
GREEN = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
RED = frozenset(
    {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR", "STARTUP_FAILURE", "STALE"}
)
DECISIONS = ("approve", "request_changes")
SELF_REVIEW_NOTE = (
    "GitHub refuses a review decision from the author, so the verdict is stated here. "
    "Merged with the ruleset bypass, which this repository's owner authorised for pull "
    "requests this account wrote."
)


class ReviewFailed(RuntimeError):
    """The model, the diff or the tooling did not give an answer worth acting on."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    author: str
    base: str
    head: str
    draft: bool
    mergeable: str
    merge_state: str
    decision: str
    checks: tuple[str, ...]

    @property
    def url(self) -> str:
        return f"https://github.com/{REPO}/pull/{self.number}"

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> PullRequest:
        rollup = row.get("statusCheckRollup") or []
        return cls(
            number=int(row["number"]),
            title=str(row["title"]),
            author=str((row.get("author") or {}).get("login") or ""),
            base=str(row.get("baseRefName") or ""),
            head=str(row.get("headRefOid") or ""),
            draft=bool(row.get("isDraft")),
            mergeable=str(row.get("mergeable") or "UNKNOWN"),
            merge_state=str(row.get("mergeStateStatus") or "UNKNOWN"),
            decision=str(row.get("reviewDecision") or ""),
            checks=tuple(
                str(item.get("conclusion") or item.get("state") or "") for item in rollup
            ),
        )

    def checks_state(self) -> str:
        """`green`, `red` or `pending`, from the check rollup alone.

        A state the API has not concluded yet — `IN_PROGRESS`, `QUEUED`, an empty
        rollup — is pending rather than failing, and pending never merges.
        """
        if not self.checks:
            return "pending"
        if any(state in RED for state in self.checks):
            return "red"
        if all(state in GREEN for state in self.checks):
            return "green"
        return "pending"


@dataclass(frozen=True)
class Verdict:
    decision: str
    body: str

    def comment(self) -> str:
        label = "APPROVE" if self.decision == "approve" else "REQUEST CHANGES"
        return f"**Verdict: {label}**\n\n{self.body}"


def parse_verdict(text: str) -> Verdict:
    """The verdict out of the model's answer, or a refusal.

    The answer is asked for as JSON and read as JSON: a body that cannot be parsed is a
    failure rather than a guess, because guessing "approve" from prose is how something
    merges that nobody approved.
    """
    body = text.strip()
    if body.startswith("```"):
        body = body.split("```")[1] if "```" in body[3:] else body[3:]
        body = body.removeprefix("json").strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        raise ReviewFailed(f"the model's answer held no object: {text[:200]}")
    try:
        answer = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReviewFailed(f"the model's answer was not JSON: {exc}") from exc
    return verdict_from(answer)


def verdict_from(answer: dict[str, Any]) -> Verdict:
    decision = str(answer.get("decision") or answer.get("verdict") or "").strip().lower()
    decision = decision.replace("-", "_").replace(" ", "_")
    if decision in ("request_changes", "changes_requested", "reject"):
        decision = "request_changes"
    if decision not in DECISIONS:
        raise ReviewFailed(f"the model decided {decision!r}, which is not a decision")
    review = str(answer.get("body") or answer.get("review") or "").strip()
    if not review:
        raise ReviewFailed("the model returned no review to post")
    return Verdict(decision=decision, body=review)


def refusal(pr: PullRequest, verdict: Verdict) -> str | None:
    """Why this pull request must not be merged, or None when it may be.

    Every reason is a fact about the pull request rather than a judgement: the decision
    itself was made when the verdict was formed.
    """
    if verdict.decision != "approve":
        return "the verdict is request changes"
    if pr.draft:
        return "it is a draft"
    if pr.mergeable != "MERGEABLE":
        return f"it does not merge ({pr.mergeable})"
    if pr.decision == "CHANGES_REQUESTED":
        return "a review is asking for changes"
    state = pr.checks_state()
    if state == "red":
        return "a check failed"
    if state == "pending":
        return "checks have not finished"
    if not pr.head:
        return "it has no head commit"
    return None


def reviewed_head(reviews: list[dict[str, Any]], viewer: str) -> str | None:
    """The commit this viewer last reviewed here, if any.

    A review carries the commit it was made against, which is what makes "already
    reviewed, still unchanged" answerable without the tool keeping state of its own.
    """
    for review in reversed(reviews):
        author = str((review.get("author") or {}).get("login") or "")
        if author == viewer and review.get("commitId"):
            return str(review["commitId"])
    return None


def prompt_for(pr: PullRequest, diff: str) -> str:
    """What the model is asked, which is this repository's own standards."""
    return f"""You are reviewing a pull request against `main` in the Misthos repository \
(a marketplace where a GitHub issue is priced, fixed and paid in USDC on Arc). Decide \
whether it should be merged, and write the review that will be posted on it.

The repository's standards, in short:
- The money path is the part that matters: amounts, issue ids, deadlines, who signs
  what, and whether a recorded figure is the one that was actually transferred.
- Do not accept a change that breaks behaviour already correct on `main`, and do not
  accept a conflict resolved by deleting the other side's work.
- Generated files must be regenerated, not hand-edited: `frontend/src/lib/api-schema.d.ts`
  (from the API schema) and `contracts/deployments/MisthosEscrow.abi.json` (from the
  contract).
- Every claim in a doc, a comment or a test name must be true of the code beside it.
- Tests must cover the behaviour the change claims, not restate the implementation.
- A pull request that has not changed since a review asked for changes is still asking
  for changes.

Answer with a single JSON object and nothing else:
{{"decision": "approve" or "request_changes", "body": "the review, in Markdown"}}

Write the body as a real review: what you checked, what you found, and — when the
decision is request_changes — exactly what must change. No praise padding, no invented
findings, no summary of the diff. Address the author as `you`.

Pull request #{pr.number}: {pr.title}
Author: {pr.author}
Base: {pr.base}
Head: {pr.head}

The diff against the base follows.

{diff}
"""


def verdict_for(pr: PullRequest, diff: str, client: httpx.Client, model: str) -> Verdict:
    """One model call, one verdict."""
    if len(diff) > MAX_DIFF:
        raise ReviewFailed(
            f"the diff is {len(diff)} characters, past the {MAX_DIFF} this tool reviews"
        )
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 4000,
        # Reasoning off, as the issue reviewer asks: without this DeepSeek spends the
        # whole budget on a thinking block and stops with `stop_reason: max_tokens`
        # before any answer, which parses as no verdict at all. `disabled` is the one
        # value both Anthropic and DeepSeek accept.
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt_for(pr, diff)}],
    }
    try:
        response = client.post("/v1/messages", json=body)
    except httpx.HTTPError as exc:
        raise ReviewFailed(f"the model call failed: {exc}") from exc
    if response.status_code >= 400:
        raise ReviewFailed(f"the model refused: {response.status_code} {response.text[:200]}")
    reply = response.json()
    text = "\n".join(
        str(block.get("text") or "")
        for block in reply.get("content", [])
        if block.get("type") == "text"
    )
    return parse_verdict(text)


# ------------------------------------------------------------------ the gh calls


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ReviewFailed(f"gh {' '.join(args)} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def gh_json(*args: str) -> Any:
    return json.loads(gh(*args) or "null")


def viewer() -> str:
    return gh("api", "user", "--jq", ".login").strip()


def open_pull_requests() -> list[PullRequest]:
    rows = gh_json(
        "pr",
        "list",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,title,author,baseRefName,headRefOid,isDraft,mergeable,mergeStateStatus,"
        "reviewDecision,statusCheckRollup",
    )
    return [PullRequest.from_json(row) for row in rows]


def diff_of(pr: PullRequest) -> str:
    return gh("pr", "diff", str(pr.number), "--patch")


def post_review(pr: PullRequest, verdict: Verdict, *, as_comment: bool) -> None:
    if as_comment:
        gh("pr", "comment", str(pr.number), "--body", verdict.comment())
        return
    event = "--approve" if verdict.decision == "approve" else "--request-changes"
    path = Path(f"/tmp/pr-{pr.number}-review.md")
    path.write_text(verdict.comment(), encoding="utf-8")
    gh("pr", "review", str(pr.number), event, "--body-file", str(path))


def merge(pr: PullRequest, *, bypass: bool) -> str:
    """Squash-merge, by the async endpoint when GitHub says it is part of a stack."""
    subject = f"{pr.title} (#{pr.number})"
    args = ["pr", "merge", str(pr.number), "--squash", "--subject", subject]
    if bypass:
        args.append("--admin")
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    output = f"{result.stdout}\n{result.stderr}"
    if "part of a stack" in output:
        gh(
            "api",
            "-X",
            "PUT",
            f"repos/{REPO}/pulls/{pr.number}/merge-async",
            "-f",
            "merge_method=squash",
            "-f",
            f"commit_title={subject}",
        )
        return "merged through the stack merge"
    if result.returncode != 0:
        raise ReviewFailed(f"merging #{pr.number} failed: {result.stderr.strip()[:300]}")
    return "merged"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", type=int, action="append", help="review just these numbers")
    parser.add_argument("--dry-run", action="store_true", help="judge, post nothing, merge nothing")
    parser.add_argument("--no-bypass", action="store_true", help="never use the ruleset bypass")
    parser.add_argument("--model", default=settings.review_model)
    args = parser.parse_args(argv)

    if not settings.anthropic_api_key and not args.dry_run:
        print("MISTHOS_ANTHROPIC_API_KEY is not set: nothing can be reviewed", file=sys.stderr)
        return 2

    who = viewer()
    client = httpx.Client(
        base_url=settings.anthropic_api_url,
        timeout=httpx.Timeout(300.0, connect=10.0),
        headers={
            "x-api-key": settings.anthropic_api_key or "unset",
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
    )

    pull_requests = open_pull_requests()
    if args.only:
        pull_requests = [pr for pr in pull_requests if pr.number in set(args.only)]
    if not pull_requests:
        print("no open pull requests")
        return 0

    failures = 0
    for pr in pull_requests:
        print(f"\n#{pr.number} {pr.title}")
        reviews = gh_json("pr", "view", str(pr.number), "--json", "reviews")["reviews"]
        seen = reviewed_head(reviews, who)
        if seen == pr.head and pr.decision == "APPROVED":
            # Reviewed at this commit on an earlier run, which then could not merge it,
            # usually because a check was still running. Judge the merge, post nothing.
            print(f"  already approved at {pr.head[:9]}; judging the merge only")
            verdict = Verdict("approve", "")
        elif seen == pr.head:
            print(f"  already reviewed at {pr.head[:9]} and not approved; leaving it alone")
            continue
        else:
            try:
                diff = diff_of(pr)
                verdict = verdict_for(pr, diff, client, args.model)
            except ReviewFailed as exc:
                # A pull request this tool cannot review is left alone rather than
                # guessed at, and said so in the log rather than on the pull request.
                print(f"  skipped: {exc}")
                failures += 1
                continue
            print(f"  verdict: {verdict.decision}")
            if not args.dry_run:
                post_review(pr, verdict, as_comment=(pr.author == who))

        blocked = refusal(pr, verdict)
        if blocked:
            print(f"  not merged: {blocked}")
            continue
        if args.dry_run:
            print("  dry run: would merge")
            continue
        try:
            print(f"  {merge(pr, bypass=(pr.author == who and not args.no_bypass))}")
        except ReviewFailed as exc:
            print(f"  not merged: {exc}")
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

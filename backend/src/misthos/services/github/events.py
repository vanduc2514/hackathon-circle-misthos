"""GitHub events, turned into lifecycle actions.

The lifecycle is event-driven in reality: a contributor opens a pull request, the
project's checks finish, the publisher merges. Each event is mapped here to the one
store action it stands for, and the store decides whether that action is legal; this
module never writes state itself.

- `issues` edited, labeled or unlabeled: `reprice`, a new proposal while the issue
  is unfunded.
- `pull_request` opened, reopened or ready_for_review: `submit_pull_request`, which
  moves `CLAIMED` to `IN_REVIEW` for the claimant's pull request only.
- `pull_request` synchronize: `submit_pull_request`, `REWORK` back to `IN_REVIEW`.
- `pull_request` closed and merged: `pull_request_closed`. Merge is acceptance, and
  the payout is released.
- `pull_request` closed without a merge: logged. The claim and the deadline decide
  what happens next.
- `check_run` completed: `record_checks`, whether the project's own checks passed.

A pull request belongs to an issue when its body closes it ("Fixes #87"), the way
GitHub itself links the two.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from misthos.domain.issue import IllegalTransition
from misthos.services.github.app import pull_request_from
from misthos.store import NotTheSubmission, Store

HANDLED_EVENTS = {
    "issues": "re-price an unfunded issue when it is edited or relabelled",
    "pull_request": "submit the claimant's pull request for review; a merge releases the payout",
    "check_run": "record whether the project's own checks passed on the submitted commit",
    "installation": "acknowledged; the App's repositories are read when an issue is published",
    "ping": "acknowledged",
}

_CLOSES = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:([\w.-]+/[\w.-]+))?#(\d+)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Handled:
    handled: bool
    outcome: str
    issue_ids: tuple[str, ...] = ()


def closed_issues(body: str, repo: str) -> list[int]:
    """The issue numbers in this repository that a pull request's body closes."""
    numbers: list[int] = []
    for match in _CLOSES.finditer(body or ""):
        other, number = match.group(1), int(match.group(2))
        if (other is None or other.lower() == repo.lower()) and number not in numbers:
            numbers.append(number)
    return numbers


def dispatch(store: Store, event: str, payload: dict[str, Any]) -> Handled:
    repo = (payload.get("repository") or {}).get("full_name")
    action = payload.get("action", "")
    if event in {"ping", "installation", "installation_repositories"}:
        return Handled(True, f"{event} acknowledged")
    if not repo:
        return Handled(False, "ignored: the payload names no repository")
    try:
        if event == "issues":
            return _issue(store, repo, action, payload)
        if event == "pull_request":
            return _pull_request(store, repo, action, payload)
        if event == "check_run":
            return _check_run(store, repo, action, payload)
    except (KeyError, TypeError, ValueError) as exc:
        return Handled(False, f"ignored: the payload is missing {exc}")
    return Handled(False, f"ignored: {event} is not an event the platform acts on")


def _issue(store: Store, repo: str, action: str, payload: dict[str, Any]) -> Handled:
    if action not in {"edited", "labeled", "unlabeled"}:
        return Handled(False, f"ignored: issues {action}")
    number = int(payload["issue"]["number"])
    rec = store.find_open(repo, number)
    if rec is None:
        return Handled(False, f"ignored: {repo}#{number} is not listed")
    before = rec.proposal.recommended if rec.proposal else None
    try:
        rec = store.reprice(rec.id)
    except IllegalTransition:
        return Handled(False, f"{rec.id} is funded, so its price stands", (rec.id,))
    after = rec.proposal.recommended if rec.proposal else None
    outcome = f"re-priced at {after}" if after != before else "price unchanged"
    return Handled(True, outcome, (rec.id,))


def _pull_request(store: Store, repo: str, action: str, payload: dict[str, Any]) -> Handled:
    pr = pull_request_from(repo, payload["pull_request"])

    if action == "closed":
        rec = store.find_by_pull_request(repo, pr.number)
        if rec is None:
            return Handled(False, f"ignored: #{pr.number} is not a submission")
        rec = store.pull_request_closed(rec.id, pr.number, merged=pr.merged)
        verb = "merged" if pr.merged else "closed"
        return Handled(True, f"#{pr.number} {verb}; {rec.id} is {rec.state.value}", (rec.id,))

    if action not in {"opened", "reopened", "ready_for_review", "synchronize"}:
        return Handled(False, f"ignored: pull_request {action}")
    if pr.draft:
        return Handled(False, f"ignored: #{pr.number} is a draft")

    moved: list[str] = []
    notes: list[str] = []
    for number in closed_issues(pr.body, repo):
        rec = store.find_open(repo, number)
        if rec is None:
            notes.append(f"#{number} is not listed")
            continue
        try:
            rec = store.submit_pull_request(rec.id, pr)
        except (IllegalTransition, NotTheSubmission) as exc:
            notes.append(f"{rec.id}: {exc}")
            continue
        moved.append(rec.id)
    if moved:
        return Handled(True, f"#{pr.number} submitted for {', '.join(moved)}", tuple(moved))
    return Handled(False, "ignored: " + ("; ".join(notes) or "it closes no listed issue"))


def _check_run(store: Store, repo: str, action: str, payload: dict[str, Any]) -> Handled:
    if action != "completed":
        return Handled(False, f"ignored: check_run {action}")
    sha = payload["check_run"]["head_sha"]
    changed = [
        rec.id
        for rec in store.list_issues()
        if rec.repo.lower() == repo.lower()
        and rec.submission is not None
        and rec.submission.head_sha == sha
        and store.record_checks(rec.id, sha)
    ]
    if changed:
        return Handled(True, f"checks recorded on {', '.join(changed)}", tuple(changed))
    return Handled(False, "no submission changed")

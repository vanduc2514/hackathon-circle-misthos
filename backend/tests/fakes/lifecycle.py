"""Test helper lifecycle transitions for integration tests."""

from __future__ import annotations

import dataclasses
import random
from datetime import datetime, UTC

from misthos.domain import issue as lifecycle, timers
from misthos.domain.issue import IssueState
from misthos.domain.money import Usdc
from misthos.models.records import IssueRecord
from misthos.services.github.base import GitHubError, PullRequest
from misthos.store import NotTheSubmission
from tests.fakes.github import SimulatedGitHub
from tests.fakes.seed import _demo_files, _fabricate_files


def _demo_pull_request_number(store, rec: IssueRecord) -> int:
    taken = {
        other.submission.pr_number
        for other in store.repo.list_issues()
        if other.id != rec.id
        and other.repo.lower() == rec.repo.lower()
        and other.submission is not None
    }
    candidate = rec.number + 400
    while candidate in taken:
        candidate += 1
    return candidate


def demo_pull_request(store, issue_id: str) -> PullRequest:
    store.ensure_ready()
    rec = store.repo.get_issue(issue_id)
    if rec is None:
        raise KeyError(issue_id)
    if rec.contributor_id is None or rec.state not in {
        IssueState.CLAIMED,
        IssueState.REWORK,
    }:
        raise lifecycle.IllegalTransition(rec.state, IssueState.IN_REVIEW)
    files = _demo_files(rec.repo)
    pr = PullRequest(
        repo=rec.repo,
        number=(
            rec.submission.pr_number if rec.submission else _demo_pull_request_number(store, rec)
        ),
        author=store._handle(rec.contributor_id),
        head_sha=f"{random.getrandbits(160):040x}",
        body=f"Fixes #{rec.number}",
        merged=False,
        files_changed=len(files),
        additions=sum(f.additions for f in files),
        deletions=sum(f.deletions for f in files),
    )
    store.github.put_pull_request(pr)
    store.github.put_files(rec.repo, pr.number, files)
    store.github.put_checks(rec.repo, pr.head_sha, True)
    return pr


def demo_merge(store, issue_id: str) -> IssueRecord:
    store.ensure_ready()
    rec = store.repo.get_issue(issue_id)
    if rec is None:
        raise KeyError(issue_id)
    if rec.submission is None:
        raise NotTheSubmission(f"{issue_id} has no pull request to merge")
    number = rec.submission.pr_number
    try:
        opened = store.github.read_pull_request(rec.repo, number)
    except GitHubError:
        opened = None
    if opened is not None:
        store.github.put_pull_request(dataclasses.replace(opened, merged=True))
    return store.pull_request_closed(issue_id, number, merged=True)



def _advance(store, issue_id: str) -> IssueRecord:
    rec = store.repo.get_issue(issue_id)
    if rec is None:
        raise KeyError(issue_id)
    if rec.state in lifecycle.TERMINAL_STATES:
        raise lifecycle.IllegalTransition(rec.state, IssueState.PAID)

    created: list[IssueRecord] = []
    now = datetime.now(UTC)
    if timers.due(store._clocks(rec), now) is not None:
        _, created = store._apply_due(rec, now)
        store.repo.save_issues(rec, *created)
        return rec

    match rec.state:
        case IssueState.AWAITING_APPROVAL:
            rec.criteria_approved_at = rec.criteria_approved_at or now
            store._approve_and_fund(
                rec, now, "approved the criteria and the price, and committed funds"
            )
        case IssueState.FUNDED:
            contributors = [c.id for c in store.repo.list_contributors()]
            cid = contributors[0]
            store._claim(rec, cid)
            store._log(
                rec,
                actor="contributor",
                action="claimed",
                rule="first_claim_wins",
                outcome=f"{store._handle(cid)} claimed the issue",
            )
        case IssueState.CLAIMED:
            store._submit(
                rec,
                {
                    "pr_number": _demo_pull_request_number(store, rec),
                    "head_sha": f"{random.getrandbits(160):040x}",
                    "checks_passed": True,
                    "files_changed": 3,
                    "additions": 62,
                    "deletions": 14,
                },
            )
            assert rec.submission is not None
            if hasattr(store, "_fabricate_files"):
                store._fabricate_files(rec)
            else:
                _fabricate_files(store, rec)
            store._log(
                rec,
                actor="contributor",
                action="submitted",
                rule="pull_request_opened",
                outcome=f"opened PR #{rec.submission.pr_number} with checks passing",
            )
        case IssueState.ACCEPTED if rec.accepted_by is None:
            store._log(
                rec,
                actor="publisher",
                action="merged",
                rule="merge_is_acceptance",
                outcome="publisher merged the pull request, which is acceptance",
            )
            rec.accepted_by = "merge"
            store._pay(rec, now)
        case IssueState.ACCEPTED:
            store._pay(rec, now)
        case IssueState.REWORK:
            store._submit(
                rec,
                {
                    "pr_number": (
                        rec.submission.pr_number
                        if rec.submission
                        else _demo_pull_request_number(store, rec)
                    ),
                    "head_sha": f"{random.getrandbits(160):040x}",
                    "checks_passed": True,
                    "files_changed": 4,
                    "additions": 88,
                    "deletions": 21,
                },
            )
            if hasattr(store, "_fabricate_files"):
                store._fabricate_files(rec)
            else:
                _fabricate_files(store, rec)
        case _:
            nxt = next(iter(lifecycle.TRANSITIONS[rec.state]), rec.state)
            raise lifecycle.IllegalTransition(rec.state, nxt)
    store.repo.save_issues(rec, *created)
    return rec


def advance(store, issue_id: str) -> IssueRecord:
    store.ensure_ready()
    current = store.repo.get_issue(issue_id)
    now = datetime.now(UTC)
    if (
        current is not None
        and current.state is IssueState.IN_REVIEW
        and timers.due(store._clocks(current), now) is None
    ):
        return store.review(issue_id) or store.get(issue_id) or current
    with store._exclusive(issue_id), store._posting():
        return _advance(store, issue_id)


def approve_and_accept(store, issue_id: str) -> IssueRecord:
    rec = store.get(issue_id)
    if rec is None:
        raise KeyError(issue_id)
    for _ in range(12):
        if rec.state in lifecycle.TERMINAL_STATES:
            break
        rec = advance(store, issue_id)
    return rec

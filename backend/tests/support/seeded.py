"""A Store seeded with the demo data, for tests only.

The seed used to live in the application. It is a fixture now: it replays the same
specifications through the store's real transitions, so the suite still starts from
the states it asserts against, and nothing in `misthos/` knows about it.
"""

from __future__ import annotations

import dataclasses
import random
from datetime import timedelta

from misthos.domain import issue as lifecycle
from misthos.domain import timers
from misthos.domain.issue import IssueState
from misthos.domain.money import Usdc
from misthos.domain.pricing import propose
from misthos.domain.signals import ComplexitySignals
from misthos.models.records import IssueRecord
from misthos.services.github import GitHubError
from misthos.services.github.base import PullRequest
from misthos.store import _PRE_FUNDING, NotTheSubmission, Store, _now
from tests.support.seed_data import _ISSUE_SPECS, _seed_contributors, _seed_publishers


def demo_files(repo: str) -> list:
    from misthos.domain.review import ChangedFile

    slug = repo.split("/")[-1].replace("-", "_")
    return [
        ChangedFile(f"src/{slug}/fix.py", additions=40, deletions=6),
        ChangedFile(f"tests/test_{slug}.py", additions=22),
        ChangedFile(f"docs/{slug}.md", additions=12),
        ChangedFile("CHANGELOG.md", additions=2),
    ]


class SeededStore(Store):
    def __init__(self, repository=None, **kwargs) -> None:
        super().__init__(repository, **kwargs)
        self._seed()

    def _seed(self) -> None:
        random.seed(7)
        for publisher in _seed_publishers().values():
            self.repo.save_publisher(publisher)
        for contributor in _seed_contributors().values():
            self.repo.save_contributor(contributor)
        for spec in _ISSUE_SPECS:
            self.repo.save_issues(self._build(spec))
        self.rebuild_standing()
        # A way in for each seeded party: the suite signs these wallets in when it
        # needs to act as that publisher or contributor.
        from tests.support.accounts import PARTY_SEEDS, account_for

        for party_id in PARTY_SEEDS:
            self.repo.save_account(account_for(party_id))

    def advance(self, issue_id: str) -> IssueRecord:
        """Move an issue one step forward along the demo path.

        A timer that is already due is the step: the demo cannot claim an issue
        whose deadline has passed or merge past a silent publisher's grace window,
        any more than real time would let it.
        """
        self.ensure_ready()
        current = self.repo.get_issue(issue_id)
        if (
            current is not None
            and current.state is IssueState.IN_REVIEW
            and timers.due(self._clocks(current), _now()) is None
        ):
            # The step is the review agent's verdict, which may take minutes and so
            # runs outside the issue's lock.
            return self.review(issue_id) or self.get(issue_id) or current
        with self._exclusive(issue_id), self._posting():
            return self._advance(issue_id)

    def _advance(self, issue_id: str) -> IssueRecord:
        rec = self.repo.get_issue(issue_id)
        if rec is None:
            raise KeyError(issue_id)
        if rec.state in lifecycle.TERMINAL_STATES:
            raise lifecycle.IllegalTransition(rec.state, IssueState.PAID)

        created: list[IssueRecord] = []
        now = _now()
        if timers.due(self._clocks(rec), now) is not None:
            _, created = self._apply_due(rec, now)
            self.repo.save_issues(rec, *created)
            return rec

        match rec.state:
            case IssueState.AWAITING_APPROVAL:
                # The demo's publisher approves the drafted criteria as they stand.
                rec.criteria_approved_at = rec.criteria_approved_at or now
                self._approve_and_fund(
                    rec, now, "approved the criteria and the price, and committed funds"
                )
            case IssueState.FUNDED:
                contributors = [c.id for c in self.repo.list_contributors()]
                cid = contributors[0]
                self._claim(rec, cid)
                self._log(
                    rec,
                    actor="contributor",
                    action="claimed",
                    rule="first_claim_wins",
                    outcome=f"{self._handle(cid)} claimed the issue",
                )
            case IssueState.CLAIMED:
                self._submit(
                    rec,
                    {
                        "pr_number": self._demo_pull_request_number(rec),
                        "head_sha": f"{random.getrandbits(160):040x}",
                        "checks_passed": True,
                        "files_changed": 3,
                        "additions": 62,
                        "deletions": 14,
                    },
                )
                assert rec.submission is not None
                self._fabricate_files(rec)
                self._log(
                    rec,
                    actor="contributor",
                    action="submitted",
                    rule="pull_request_opened",
                    outcome=f"opened PR #{rec.submission.pr_number} with checks passing",
                )
            case IssueState.ACCEPTED if rec.accepted_by is None:
                # Merging is acceptance, and acceptance is what releases the money,
                # so the merge goes on the record before the release does.
                self._log(
                    rec,
                    actor="publisher",
                    action="merged",
                    rule="merge_is_acceptance",
                    outcome="publisher merged the pull request, which is acceptance",
                )
                rec.accepted_by = "merge"
                self._pay(rec, now)
            case IssueState.ACCEPTED:
                # Accepted, and the payout is held: the step is checking it again.
                self._pay(rec, now)
            case IssueState.REWORK:
                self._submit(
                    rec,
                    {
                        "pr_number": (
                            rec.submission.pr_number
                            if rec.submission
                            else self._demo_pull_request_number(rec)
                        ),
                        "head_sha": f"{random.getrandbits(160):040x}",
                        "checks_passed": True,
                        "files_changed": 4,
                        "additions": 88,
                        "deletions": 21,
                    },
                )
                self._fabricate_files(rec)
            case _:
                # DRAFT, PRICED and REJECTED are legal states with no demo leg out of
                # them. Refuse with the state the lifecycle declares next rather than
                # returning an unchanged record behind a 200.
                nxt = next(iter(lifecycle.TRANSITIONS[rec.state]), rec.state)
                raise lifecycle.IllegalTransition(rec.state, nxt)
        self.repo.save_issues(rec, *created)
        return rec

    def approve_and_accept(self, issue_id: str) -> IssueRecord:
        """Run the remainder of the happy path in one call, for demos."""
        rec = self.get(issue_id)
        if rec is None:
            raise KeyError(issue_id)
        for _ in range(12):
            if rec.state in lifecycle.TERMINAL_STATES:
                break
            rec = self.advance(issue_id)
        return rec

    def _demo_pull_request_number(self, rec: IssueRecord) -> int:
        """The number of the first pull request the simulation opens for an issue: its own
        number plus 400, or the next one up that no other issue's submission in the
        repository has, since a pull request counts toward one issue only (#120)."""
        taken = {
            other.submission.pr_number
            for other in self.repo.list_issues()
            if other.id != rec.id
            and other.repo.lower() == rec.repo.lower()
            and other.submission is not None
        }
        number = rec.number + 400
        while number in taken:
            number += 1
        return number

    def ensure_ready(self) -> None:
        """The seed is already in place; the old lifecycle calls are harmless."""

    def demo_pull_request(self, issue_id: str) -> PullRequest:
        """Open, on the simulated GitHub, the pull request the claimant would open on
        the real one: a fix, its test, docs and a changelog line, checks passing.
        After rework it is a new commit on the same pull request. Submitting it is
        still the claimant's explicit action."""
        self.ensure_ready()
        if not hasattr(self.github, "put_pull_request"):
            raise RuntimeError("the fake GitHub opens pull requests for you")
        rec = self.repo.get_issue(issue_id)
        if rec is None:
            raise KeyError(issue_id)
        if rec.contributor_id is None or rec.state not in {
            IssueState.CLAIMED,
            IssueState.REWORK,
        }:
            raise lifecycle.IllegalTransition(rec.state, IssueState.IN_REVIEW)
        files = demo_files(rec.repo)
        pr = PullRequest(
            repo=rec.repo,
            number=(
                rec.submission.pr_number if rec.submission else self._demo_pull_request_number(rec)
            ),
            author=self._handle(rec.contributor_id),
            head_sha=f"{random.getrandbits(160):040x}",
            body=f"Fixes #{rec.number}",
            merged=False,
            files_changed=len(files),
            additions=sum(f.additions for f in files),
            deletions=sum(f.deletions for f in files),
        )
        self.github.put_pull_request(pr)
        self.github.put_files(rec.repo, pr.number, files)
        self.github.put_checks(rec.repo, pr.head_sha, True)
        return pr

    def demo_merge(self, issue_id: str) -> IssueRecord:
        """Merge the submitted pull request on the simulated GitHub, as the publisher
        would on the real one, and handle it as that merge's webhook is handled."""
        self.ensure_ready()
        if not hasattr(self.github, "put_pull_request"):
            raise RuntimeError("merge the pull request through the fake GitHub")
        rec = self.repo.get_issue(issue_id)
        if rec is None:
            raise KeyError(issue_id)
        if rec.submission is None:
            raise NotTheSubmission(f"{issue_id} has no pull request to merge")
        number = rec.submission.pr_number
        try:
            opened = self.github.read_pull_request(rec.repo, number)
        except GitHubError:
            opened = None
        if opened is not None:
            self.github.put_pull_request(dataclasses.replace(opened, merged=True))
        return self.pull_request_closed(issue_id, number, merged=True)

    def reset(self) -> None:
        from misthos.repositories import MemoryRepository

        self.repo = MemoryRepository()
        for party in (self.chain, self.github):
            reset = getattr(party, "reset", None)
            if reset is not None:
                reset()
        self._seed()

    def _fabricate_files(self, rec: IssueRecord) -> None:
        if rec.submission is None:
            return
        self.github.put_files(rec.repo, rec.submission.pr_number, demo_files(rec.repo))

    def _build(self, spec: dict) -> IssueRecord:
        publisher = self.repo.get_publisher(spec["publisher_id"])
        assert publisher is not None, spec["publisher_id"]
        signals = ComplexitySignals(**spec["signals"])
        proposal = propose(
            signals,
            compliance_driven=spec.get("compliance_driven", False),
            comparables=self._comparables_for(signals, spec["repo"]),
            urgency=Usdc.from_decimal(spec["urgency"]) if spec.get("urgency") else None,
            risk_premium=Usdc.from_decimal(spec["risk"]) if spec.get("risk") else None,
        )
        target = IssueState(spec["state"])
        now = _now()
        if "price" in spec:
            price = Usdc.from_decimal(spec["price"]) if spec["price"] else None
        else:
            price = proposal.recommended
        rec = IssueRecord(
            id=self._next_issue_id(),
            repo=spec["repo"],
            number=spec["number"],
            title=spec["title"],
            summary=spec["summary"],
            state=target if target in _PRE_FUNDING else IssueState.AWAITING_APPROVAL,
            labels=spec["labels"],
            compliance_driven=spec.get("compliance_driven", False),
            acceptance_criteria=spec["criteria"],
            publisher_id=spec["publisher_id"],
            created_at=now - timedelta(days=spec["age_days"]),
            proposal=proposal,
            price=price,
        )

        if spec.get("signals_read"):
            self._log(
                rec,
                actor="agent",
                action="signals_read",
                rule="github_read_path",
                outcome=spec["signals_read"],
            )
        self._log(
            rec,
            actor="agent",
            action="price_suggested",
            rule="complexity_score_v1",
            outcome=(
                f"suggested {proposal.recommended} for {proposal.estimated_hours}h; "
                "advice only, the publisher's price is what binds"
            ),
            cost="0.00",
        )

        if target in _PRE_FUNDING:
            return rec

        rec.criteria_approved_at = rec.created_at + timedelta(hours=5)
        self._fund(rec, when=rec.created_at + timedelta(hours=6))
        self._log(
            rec,
            actor="publisher",
            action="price_approved",
            rule="human_checkpoint",
            outcome=f"approved {rec.price} and committed the funds",
        )

        if target is IssueState.FUNDED:
            return rec

        if target is IssueState.REFUNDED:
            self._refund(rec, rule="deadline_passed")
            return rec

        contributor_id = spec["contributor_id"]
        self._claim(rec, contributor_id, when=rec.created_at + timedelta(hours=20))
        self._log(
            rec,
            actor="contributor",
            action="claimed",
            rule="first_claim_wins",
            outcome=f"{self._handle(contributor_id)} took an exclusive 72h claim",
        )

        if target is IssueState.CLAIMED:
            return rec

        self._submit(rec, spec["pr"])
        self._fabricate_files(rec)

        if target is IssueState.IN_REVIEW:
            return rec

        self._review(
            rec,
            verdict=spec["verdict"],
            findings=spec["findings"],
            when=rec.created_at + timedelta(hours=44),
        )

        if target is IssueState.REWORK:
            self._log(
                rec,
                actor="agent",
                action="verdict_issued",
                rule="criteria_unmet",
                outcome="rework by review_agent_v1: " + " ".join(spec["findings"]),
                when=rec.created_at + timedelta(hours=44),
            )
            return rec

        self._log(
            rec,
            actor="agent",
            action="verdict_passed",
            rule="review_agent_v1",
            outcome="verdict passed, awaiting the publisher's merge",
            when=rec.created_at + timedelta(hours=45),
        )

        if target is IssueState.ACCEPTED:
            return rec

        rec.accepted_by = "merge"
        self._release(rec, when=rec.created_at + timedelta(hours=45, minutes=2))
        return rec


__all__ = ["SeededStore", "demo_files"]

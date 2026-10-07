"""The issue aggregate, as the store and the repositories exchange it.

A funded issue is one consistency boundary: its state, price, commitment, claim,
submission, verdict and decision log change together and are saved together. The
record lives here rather than in the store so a repository can persist it without
importing the module that mutates it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from misthos.domain.issue import IssueState
from misthos.domain.money import Usdc
from misthos.domain.pricing import PriceProposal
from misthos.schemas import Claim, Decision, EscrowCommitment, Review, Submission


@dataclass
class IssueRecord:
    id: str
    repo: str
    number: int
    title: str
    summary: str
    state: IssueState
    labels: list[str]
    compliance_driven: bool
    acceptance_criteria: list[str]
    publisher_id: str
    created_at: datetime
    deadline: datetime | None = None
    proposal: PriceProposal | None = None
    escrow: EscrowCommitment | None = None
    claim: Claim | None = None
    submission: Submission | None = None
    review: Review | None = None
    contributor_id: str | None = None
    paid: Usdc | None = None
    relisted_from: str | None = None
    """The issue this one re-lists at a higher band after nobody claimed it."""
    decisions: list[Decision] = field(default_factory=list)
    version: int = 0
    """Optimistic-concurrency token. Zero means never saved. Every save bumps it, and
    a save made from a copy someone else has saved since is refused, so a person and
    the sweeper acting on the same issue cannot silently overwrite each other."""

    @property
    def github_url(self) -> str:
        return f"https://github.com/{self.repo}/issues/{self.number}"

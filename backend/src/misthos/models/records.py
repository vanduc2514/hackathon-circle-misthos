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
from misthos.domain.ledger import MoneyEvent
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
    platform_fee: Usdc | None = None
    """The platform's take rate carved out of the settlement (#32). The contributor
    received `paid`; the two add up to the commitment."""
    paid_at: datetime | None = None
    payout_tx_hash: str | None = None
    """The release transfer. Kept off every public view: with the contributor's handle
    on the issue page it would link their wallet to their GitHub identity."""
    accepted_by: str | None = None
    """What accepted the work and entitled the contributor to payment: a merge, or the
    publisher's silence past the grace period. Set once, before the payout."""
    payout_hold: str | None = None
    """Why an entitled payout has not left the escrow yet, from the compliance gate."""
    payout_checked_at: datetime | None = None
    criteria_approved_at: datetime | None = None

    @property
    def received(self) -> Usdc:
        """What the contributor received: the commitment less the platform's take (#32).

        The RELEASED event is the whole commitment, because that is what leaves the
        escrow and what the chain settles against, so every figure about what a
        contributor earned reads this and not the event.
        """
        return self.paid if self.paid is not None else Usdc(0)
    """When the publisher approved the acceptance criteria. No funding before it (#21)."""
    relisted_from: str | None = None
    """The issue this one re-lists at a higher band after nobody claimed it."""
    decisions: list[Decision] = field(default_factory=list)
    money_events: list[MoneyEvent] = field(default_factory=list)
    """Every movement of this issue's money, in order. Appended, never edited."""
    version: int = 0
    """Optimistic-concurrency token. Zero means never saved. Every save bumps it, and
    a save made from a copy someone else has saved since is refused, so a person and
    the sweeper acting on the same issue cannot silently overwrite each other."""

    @property
    def github_url(self) -> str:
        return f"https://github.com/{self.repo}/issues/{self.number}"

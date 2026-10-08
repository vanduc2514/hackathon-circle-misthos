"""The Arc settlement path against the compiled MisthosEscrow on a local anvil.

Each case here was found by running the store against the contract rather than the
simulation (#121 to #127), so each is proven the same way: the store signs as the
platform does, the publisher's own wallet sends the commitment the plan spells out,
and every balance is read back from the token. Skipped where Foundry is not installed.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta

import pytest
from local_arc import ADDRESS, GRANT, LocalArc, unavailable

from misthos.config import settings
from misthos.domain.escrow import selector
from misthos.domain.issue import RELEASE_MARGIN, FundingTerms, IssueState
from misthos.domain.ledger import EscrowStatus
from misthos.domain.money import Usdc
from misthos.models.records import IssueRecord
from misthos.repositories import MemoryRepository
from misthos.schemas import PublishRequest, Wallet
from misthos.services.billing import SimulatedRail
from misthos.services.chain import ArcEscrow, ChainRevert, NotCommitted
from misthos.services.chain.arc import NO_FEE_RECIPIENT
from misthos.services.coordination import build_coordinator
from misthos.services.github import SimulatedGitHub
from misthos.services.wallets import simulated_address
from misthos.store import DeclineRefused, Store
from misthos.workers.sweeper import sweep_once

pytestmark = pytest.mark.skipif(unavailable() is not None, reason=str(unavailable()))

FAR_FUTURE = 4_102_444_800  # 1 January 2100


@pytest.fixture
def arc() -> Iterator[LocalArc]:
    chain = LocalArc.start()
    yield chain
    chain.stop()


@dataclass
class Platform:
    arc: LocalArc
    store: Store
    publisher_id: str
    contributor_id: str

    @property
    def escrow(self) -> ArcEscrow:
        assert isinstance(self.store.chain, ArcEscrow)
        return self.store.chain


def platform(arc: LocalArc) -> Platform:
    """A store settling through the local escrow, with one publisher and one contributor
    who signed in with their own wallets."""
    repo = MemoryRepository()
    stores: list[Store] = []
    store = Store(
        repo,
        chain=arc.gateway(lambda: [r.id for r in stores[0].repo.list_issues()]),
        github=SimulatedGitHub(),
        coordinator=build_coordinator("", repo),
        rail=SimulatedRail(),
    )
    stores.append(store)
    publisher = store.create_account(ADDRESS["publisher"], "publisher", "Arc Pub")
    store.link_github(publisher.address, "arc-pub")
    contributor = store.create_account(ADDRESS["contributor"], "contributor", "arc-con")
    store.link_github(contributor.address, "arc-con")
    return Platform(arc, store, publisher.party_id, contributor.party_id)


def publish(p: Platform) -> str:
    rec = p.store.publish(
        PublishRequest(
            repo="arc/repo",
            title="Fix the retry backoff in the client",
            publisher_id=p.publisher_id,
            summary="Add a regression test and fix it.",
        )
    )
    p.store.approve_criteria(rec.id, rec.acceptance_criteria, "arc-pub")
    return rec.id


def approve(p: Platform, issue_id: str) -> FundingTerms:
    """The first approval: the terms go on the escrow, which waits for the wallet."""
    with pytest.raises(NotCommitted):
        p.store.approve_price(issue_id, "arc-pub", now=p.arc.now())
    rec = p.store.get(issue_id)
    assert rec is not None and rec.funding is not None
    return rec.funding


def fund(p: Platform, issue_id: str) -> IssueRecord:
    terms = approve(p, issue_id)
    p.arc.wallet_commits("publisher", issue_id, terms.amount, int(terms.deadline.timestamp()))
    funded = p.store.approve_price(issue_id, "arc-pub", now=p.arc.now())
    assert funded.state is IssueState.FUNDED
    return funded


def accept(p: Platform, issue_id: str) -> IssueRecord:
    p.store.claim(issue_id, p.contributor_id)
    p.store.submit_pull_request(issue_id, p.store.demo_pull_request(issue_id))
    rec = p.store.review(issue_id)
    assert rec is not None and rec.state is IssueState.ACCEPTED
    return rec


def verdict_at(p: Platform, issue_id: str, before_deadline: timedelta) -> IssueRecord:
    """Move the passing verdict to this long before the escrow deadline."""
    rec = p.store.get(issue_id)
    assert rec is not None and rec.review is not None and rec.deadline is not None
    rec.review.decided_at = rec.deadline - before_deadline
    p.store.save(rec)
    return rec


def _word(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


class TestTheSilentPublisherRelease:
    """#121: the capped release fired once `now > deadline`, when `release` reverts."""

    def test_a_verdict_with_two_days_left_is_paid_before_the_deadline(self, arc: LocalArc) -> None:
        p = platform(arc)
        issue_id = publish(p)
        price = fund(p, issue_id).proposal.recommended  # type: ignore[union-attr]
        accept(p, issue_id)
        rec = verdict_at(p, issue_id, timedelta(days=2))
        assert rec.deadline is not None

        arc.warp_to(rec.deadline - RELEASE_MARGIN + timedelta(seconds=30))
        report = sweep_once(p.store, now=arc.now())

        assert report.applied[issue_id] == ["release_after_grace"]
        paid = p.store.get(issue_id)
        assert paid is not None and paid.state is IssueState.PAID
        assert paid.paid is not None and paid.platform_fee is not None
        assert arc.balance("contributor") == paid.paid
        assert arc.balance("treasury") == paid.platform_fee
        assert paid.paid + paid.platform_fee == price
        assert p.escrow.commitment(issue_id).status is EscrowStatus.RELEASED  # type: ignore[union-attr]
        assert report.divergences == 0

    def test_accepted_work_the_deadline_overtook_follows_the_chain_to_a_refund(
        self, arc: LocalArc
    ) -> None:
        p = platform(arc)
        issue_id = publish(p)
        fund(p, issue_id)
        accept(p, issue_id)
        rec = verdict_at(p, issue_id, timedelta(minutes=1))
        assert rec.deadline is not None

        # Nobody swept inside the margin: by the next pass the release cannot land.
        arc.warp_to(rec.deadline + timedelta(seconds=60))
        report = sweep_once(p.store, now=arc.now())

        assert report.applied[issue_id] == ["refund"]
        refunded = p.store.get(issue_id)
        assert refunded is not None and refunded.state is IssueState.REFUNDED
        assert arc.balance("publisher") == GRANT
        assert p.escrow.commitment(issue_id).status is EscrowStatus.REFUNDED  # type: ignore[union-attr]
        assert p.store.reconcile() == []


class TestOnlyTheApprovedPublisherCommits:
    """#122: anyone could commit dust first and keep the issue from ever being funded."""

    def test_a_stranger_cannot_squat_the_issue_and_the_publisher_still_funds_it(
        self, arc: LocalArc
    ) -> None:
        p = platform(arc)
        issue_id = publish(p)
        terms = approve(p, issue_id)

        with pytest.raises(ChainRevert, match="NotApprovedPublisher"):
            arc.wallet_commits("stranger", issue_id, Usdc(1), FAR_FUTURE)
        arc.wallet_commits("publisher", issue_id, terms.amount, int(terms.deadline.timestamp()))
        funded = p.store.approve_price(issue_id, "arc-pub", now=arc.now())

        assert funded.state is IssueState.FUNDED
        held = p.escrow.commitment(issue_id)
        assert held is not None and held.publisher == ADDRESS["publisher"].lower()
        assert arc.balance("stranger") == GRANT

    def test_a_deadline_past_the_approved_one_is_refused_by_the_contract(
        self, arc: LocalArc
    ) -> None:
        p = platform(arc)
        issue_id = publish(p)
        terms = approve(p, issue_id)
        with pytest.raises(ChainRevert, match="DeadlineTooLate"):
            arc.wallet_commits("publisher", issue_id, terms.amount, FAR_FUTURE)
        assert p.escrow.commitment(issue_id) is None


class TestTheWalletAPublisherFundsFrom:
    """#123: setting up a Circle wallet replaced the funding wallet, so the booking of
    a commitment from the browser wallet was refused WrongPublisher."""

    def test_a_publisher_with_a_circle_wallet_funds_from_the_wallet_they_sign_in_with(
        self, arc: LocalArc
    ) -> None:
        p = platform(arc)
        circle = Wallet(
            address=simulated_address("publisher", p.publisher_id), chain=settings.chain
        )
        p.store.link_wallet("publisher", p.publisher_id, circle)
        issue_id = publish(p)

        funded = fund(p, issue_id)

        assert funded.state is IssueState.FUNDED
        held = p.escrow.commitment(issue_id)
        assert held is not None and held.publisher == ADDRESS["publisher"].lower()
        publisher = p.store.get_publisher(p.publisher_id)
        assert publisher is not None
        assert publisher.wallet.address == ADDRESS["publisher"].lower()
        assert publisher.circle_wallet == circle


class TestAMergeTheChainRefuses:
    """#125: a release that reverted threw the merge away and let the publisher decline."""

    def test_the_merge_stands_while_the_release_reverts_and_pays_once_it_lands(
        self, arc: LocalArc
    ) -> None:
        p = platform(arc)
        issue_id = publish(p)
        fund(p, issue_id)
        accept(p, issue_id)
        set_attestor = selector("setAttestor(address)")
        # The attestor key is rotated away mid-flight: every release reverts NotAttestor.
        arc.send("owner", arc.escrow, set_attestor + _word(ADDRESS["stranger"]))

        merged = p.store.demo_merge(issue_id)

        assert merged.state is IssueState.ACCEPTED and merged.accepted_by == "merge"
        assert merged.payout_hold == "release_failed"
        assert "NotAttestor" in merged.decisions[-1].outcome
        with pytest.raises(DeclineRefused, match="merged"):
            p.store.decline(issue_id, "Changed my mind after merging.")

        arc.send("owner", arc.escrow, set_attestor + _word(ADDRESS["attestor"]))
        arc.warp_to(arc.now() + timedelta(minutes=1))
        sweep_once(p.store, now=arc.now())

        paid = p.store.get(issue_id)
        assert paid is not None and paid.state is IssueState.PAID
        assert arc.balance("contributor") == paid.paid


class TestBookingHonoursTheApproval:
    """#126: booking recomputed the deadline and the rate, and refused for good."""

    def test_a_booking_ninety_minutes_after_the_plan_still_books(self, arc: LocalArc) -> None:
        p = platform(arc)
        issue_id = publish(p)
        terms = approve(p, issue_id)
        arc.wallet_commits("publisher", issue_id, terms.amount, int(terms.deadline.timestamp()))

        arc.warp_to(arc.now() + timedelta(minutes=90))
        funded = p.store.approve_price(issue_id, "arc-pub", now=arc.now())

        assert funded.state is IssueState.FUNDED
        assert funded.deadline == terms.deadline

    def test_a_plan_change_before_booking_books_at_the_approved_rate(self, arc: LocalArc) -> None:
        p = platform(arc)
        issue_id = publish(p)
        terms = approve(p, issue_id)
        assert terms.fee_bps == 1200  # Open
        p.store.set_contract_plan(p.publisher_id, "team", arc.now() + timedelta(days=30))
        arc.wallet_commits("publisher", issue_id, terms.amount, int(terms.deadline.timestamp()))

        funded = p.store.approve_price(issue_id, "arc-pub", now=arc.now())

        assert funded.state is IssueState.FUNDED
        assert funded.escrow is not None and funded.escrow.fee_bps == 1200
        assert p.escrow.fee_bps(issue_id) == 1200

    def test_a_commitment_that_can_never_be_booked_goes_back_at_its_deadline(
        self, arc: LocalArc
    ) -> None:
        p = platform(arc)
        issue_id = publish(p)
        terms = approve(p, issue_id)
        # The wallet sends another amount than the approval named.
        arc.wallet_commits("publisher", issue_id, Usdc(1), int(terms.deadline.timestamp()))
        with pytest.raises(ChainRevert, match="AmountMismatch"):
            p.store.approve_price(issue_id, "arc-pub", now=arc.now())

        arc.warp_to(terms.deadline + timedelta(seconds=60))
        report = sweep_once(p.store, now=arc.now())

        assert report.applied[issue_id] == ["lapse_approval"]
        back = p.store.get(issue_id)
        assert back is not None and back.state is IssueState.REFUNDED
        assert arc.balance("publisher") == GRANT
        assert p.store.reconcile() == []


class TestAnEscrowWithNoFeeRecipient:
    """#127: deployed as the docs said, every release reverted FeeRecipientNotSet."""

    def test_funding_is_refused_before_any_money_is_committed(self) -> None:
        arc = LocalArc.start(fee_recipient=False)
        try:
            p = platform(arc)
            issue_id = publish(p)
            assert p.escrow.settlement_problems() == [NO_FEE_RECIPIENT]

            with pytest.raises(ChainRevert, match="FeeRecipientNotSet"):
                p.store.approve_price(issue_id, "arc-pub", now=arc.now())
            # Nothing reached the escrow, so no wallet could commit to it.
            assert p.escrow.escrow_ceiling(issue_id) is None
            assert p.store.get(issue_id).funding is None  # type: ignore[union-attr]

            treasury = selector("setFeeRecipient(address)") + _word(ADDRESS["treasury"])
            arc.send("owner", arc.escrow, treasury)
            assert p.escrow.settlement_problems() == []
            approve(p, issue_id)
        finally:
            arc.stop()

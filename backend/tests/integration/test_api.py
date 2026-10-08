"""API tests against the seeded simulation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from siwe_wallet import Wallet

from misthos.config import settings
from misthos.domain.issue import ESCROW_TERM, IssueState
from misthos.domain.money import Usdc
from misthos.domain.pricing import take_rate_bps
from misthos.main import app
from misthos.repositories import StaleIssue
from misthos.services.chain import ChainRevert
from misthos.services.wallets import simulated_address
from misthos.store import store

API = "/api/v1"


def sign_in(client: TestClient, seed: int, role: str, name: str, login: str) -> dict:
    """A signed-in account with a role and a linked GitHub login, for the guards."""
    wallet = Wallet(seed)
    nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
    message = wallet.message(nonce)
    verified = client.post(
        f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)}
    )
    assert verified.status_code == 200, verified.text
    account = client.post(f"{API}/auth/role", json={"role": role, "name": name})
    assert account.status_code == 201, account.text
    linked = client.post(f"{API}/auth/github/simulate", json={"login": login})
    assert linked.status_code == 200, linked.text
    return linked.json()


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    yield

@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class TestEscrowReadback:
    def _first(self, client: TestClient, state: str) -> str:
        rows = client.get(f"{API}/issues", params={"state": state}).json()
        assert rows, f"seed has no {state} issue"
        return rows[0]["id"]

    def test_a_funded_issue_reads_back_from_the_escrow_books(self, client: TestClient) -> None:
        issue_id = self._first(client, "funded")
        body = client.get(f"{API}/issues/{issue_id}/escrow").json()
        issue = client.get(f"{API}/issues/{issue_id}").json()

        assert body["source"] == "simulation"
        assert body["status"] == "held"
        assert body["amount"] == issue["escrow"]["amount"]
        assert len(body["issue_key"]) == 66
        assert body["explorer_url"].endswith(body["contract"])

    def test_an_issue_never_committed_reads_back_as_none(self, client: TestClient) -> None:
        body = client.get(
            f"{API}/issues/{self._first(client, 'awaiting_approval')}/escrow"
        ).json()
        assert body["status"] == "none"
        assert body["publisher"] is None

    def test_the_answer_is_the_escrow_not_the_issue_record(self, client: TestClient) -> None:
        # Move the escrow without the platform, as a compromised key would: the
        # readback must report the escrow's state, not the platform's memory.
        from misthos.domain.ledger import EscrowStatus

        issue_id = self._first(client, "funded")
        store.chain.tamper(issue_id, EscrowStatus.REFUNDED)  # type: ignore[attr-defined]
        assert client.get(f"{API}/issues/{issue_id}/escrow").json()["status"] == "refunded"

    def test_an_unknown_issue_is_404(self, client: TestClient) -> None:
        assert client.get(f"{API}/issues/ISS-9999/escrow").status_code == 404

    def _awaiting_commitment(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> str:
        """An issue whose price is approved while the escrow waits for the publisher's
        own wallet, as on Arc: the first approval records the terms and is refused."""
        from misthos.services.chain import NotCommitted

        issue_id = self._first(client, "awaiting_approval")
        client.post(f"{API}/issues/{issue_id}/criteria", json={
            "criteria": client.get(f"{API}/issues/{issue_id}").json()["acceptance_criteria"]
        })

        def not_yet(*_: object) -> str:
            raise NotCommitted("NotCommitted: the publisher has not committed this issue yet")

        monkeypatch.setattr(store.chain, "commit", not_yet)
        refused = client.post(f"{API}/issues/{issue_id}/fund")
        assert refused.status_code == 409
        assert "NotCommitted" in refused.json()["detail"]
        return issue_id

    def test_funding_waits_for_the_publishers_own_commitment(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On Arc the escrow refuses to book funding the publisher has not committed
        # from their wallet; the web app recognises this refusal by its name.
        issue_id = self._awaiting_commitment(client, monkeypatch)
        assert client.get(f"{API}/issues/{issue_id}").json()["state"] == "AWAITING_APPROVAL"
        # The ceiling was recorded before the refusal: the commitment can now be sent.
        assert client.get(f"{API}/issues/{issue_id}/escrow").json()["escrow_ceiling"]

        monkeypatch.undo()
        funded = client.post(f"{API}/issues/{issue_id}/fund")
        assert funded.status_code == 200, funded.text
        assert funded.json()["state"] == "FUNDED"

    def test_the_publisher_is_told_exactly_what_their_wallet_sends(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from misthos.domain.escrow import approve_call, commit_call
        from misthos.domain.money import Usdc

        issue_id = self._awaiting_commitment(client, monkeypatch)
        plan = client.get(f"{API}/issues/{issue_id}/commitment").json()
        amount = Usdc(plan["amount"]["base_units"])

        assert plan["escrow"] == client.get(f"{API}/issues/{issue_id}/escrow").json()["contract"]
        approve, commit = plan["calls"]
        # First the USDC allowance, then the commitment, to the addresses that hold them.
        assert (approve["to"], approve["data"]) == (
            plan["usdc"],
            approve_call(plan["escrow"], amount),
        )
        assert (commit["to"], commit["data"]) == (
            plan["escrow"],
            commit_call(issue_id, amount, plan["deadline"]),
        )
        assert amount.base_units == client.get(f"{API}/issues/{issue_id}").json()["proposal"][
            "recommended"
        ]["base_units"]

    def test_the_plan_is_the_approved_terms_and_does_not_move_with_the_clock(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The plan used to compute its deadline from now, and the booking computed its
        own, so a booking more than an hour after the plan was refused for good with the
        money already committed (#126). The plan is now the approval's own terms."""
        issue_id = self._awaiting_commitment(client, monkeypatch)
        rec = store.get(issue_id)
        assert rec is not None and rec.funding is not None
        publisher = store.get_publisher(rec.publisher_id)
        assert publisher is not None

        first = client.get(f"{API}/issues/{issue_id}/commitment").json()
        # An hour and a half later, approving again keeps the same terms.
        later = rec.funding.approved_at + timedelta(minutes=90)
        with pytest.raises(Exception, match="NotCommitted"):
            store.approve_price(issue_id, "the publisher", now=later)
        second = client.get(f"{API}/issues/{issue_id}/commitment").json()

        assert first == second
        assert first["deadline"] == int(rec.funding.deadline.timestamp())
        assert first["fee_bps"] == take_rate_bps(publisher.tier)
        assert first["wallet"] == publisher.wallet.address

    def test_there_is_no_plan_before_the_price_is_approved(self, client: TestClient) -> None:
        issue_id = self._first(client, "awaiting_approval")
        refused = client.get(f"{API}/issues/{issue_id}/commitment")
        assert refused.status_code == 409
        assert "approve the price" in refused.json()["detail"]

    def test_a_funded_issue_is_capped_at_its_approved_price(self, client: TestClient) -> None:
        issue_id = self._first(client, "funded")
        body = client.get(f"{API}/issues/{issue_id}/escrow").json()
        issue = client.get(f"{API}/issues/{issue_id}").json()
        assert body["escrow_ceiling"] == issue["proposal"]["recommended"]
        assert body["amount"]["base_units"] <= body["escrow_ceiling"]["base_units"]

    def test_an_issue_nobody_approved_has_no_ceiling(self, client: TestClient) -> None:
        issue_id = self._first(client, "awaiting_approval")
        assert client.get(f"{API}/issues/{issue_id}/escrow").json()["escrow_ceiling"] is None

    def test_the_ceiling_is_the_escrow_s_not_a_copy_of_the_amount(
        self, client: TestClient
    ) -> None:
        # Change the ceiling on the escrow alone: the readback must follow the escrow,
        # which it could not if the ceiling were derived from the committed amount.
        from datetime import UTC, datetime

        from misthos.domain.money import Usdc

        issue_id = self._first(client, "funded")
        now = datetime.now(UTC)
        store.chain.set_ceiling(
            issue_id,
            Usdc(999_000_000),
            now,
            publisher="0x" + "b0" * 20,
            latest_deadline=now + timedelta(days=14),
        )
        body = client.get(f"{API}/issues/{issue_id}/escrow").json()
        assert body["escrow_ceiling"]["base_units"] == 999_000_000
        assert body["amount"]["base_units"] != 999_000_000

    def test_approving_the_price_records_the_ceiling_before_committing(
        self, client: TestClient
    ) -> None:
        issue_id = self._first(client, "awaiting_approval")
        approved = client.get(f"{API}/issues/{issue_id}").json()["proposal"]["recommended"]
        store.advance(issue_id)
        body = client.get(f"{API}/issues/{issue_id}/escrow").json()
        assert body["status"] == "held"
        assert body["escrow_ceiling"] == approved


class TestHealth:
    def test_health_reports_the_simulation(self, client: TestClient) -> None:
        body = client.get(f"{API}/health").json()
        assert body["status"] == "ok"
        assert body["simulated"] is True
        assert body["seeded_issues"] == 8
        assert body["chain"] == "arc-testnet"
        assert body["chain_id"] == 5042002
        assert body["money"] == "simulated"
        assert "no money moves" in body["money_note"]

    def test_the_label_follows_the_chain_id_at_request_time(self, client: TestClient) -> None:
        original = (settings.chain_id, settings.simulated)
        try:
            settings.chain_id, settings.simulated = 5042, False
            mainnet = client.get(f"{API}/health").json()
            settings.chain_id = 8453
            other = client.get(f"{API}/health").json()
        finally:
            settings.chain_id, settings.simulated = original
        assert (mainnet["chain"], mainnet["money"]) == ("arc-mainnet", "real")
        assert (other["chain"], other["money"]) == ("chain-8453", "unknown")


class TestIssues:
    def test_lists_every_seeded_issue(self, client: TestClient) -> None:
        assert len(client.get(f"{API}/issues").json()) == 8

    def test_filters_by_state(self, client: TestClient) -> None:
        rows = client.get(f"{API}/issues", params={"state": "funded"}).json()
        assert rows and all(r["state"] == "FUNDED" for r in rows)

    def test_unknown_state_is_rejected(self, client: TestClient) -> None:
        assert client.get(f"{API}/issues", params={"state": "nonsense"}).status_code == 400

    def test_filters_compliance_driven(self, client: TestClient) -> None:
        rows = client.get(f"{API}/issues", params={"compliance_only": True}).json()
        assert rows and all(r["compliance_driven"] for r in rows)

    def test_detail_carries_the_price_and_its_reasoning(self, client: TestClient) -> None:
        body = client.get(f"{API}/issues/ISS-1001").json()
        assert body["proposal"]["recommended"]["usdc"]
        assert body["proposal"]["justification"]
        assert body["proposal"]["confidence"] in {"low", "medium", "high"}

    def test_missing_issue_is_404(self, client: TestClient) -> None:
        assert client.get(f"{API}/issues/NOPE").status_code == 404

    def test_timeline_is_a_decision_log(self, client: TestClient) -> None:
        rows = client.get(f"{API}/issues/ISS-1002/timeline").json()
        assert rows
        assert {"actor", "action", "outcome"} <= rows[0].keys()


class TestBookingHonoursTheApproval:
    """Booking checks the commitment against the terms the publisher approved, never
    against ones worked out again at booking (#126). The simulated escrow plays Arc's
    two approvals here: the first is refused NotCommitted while the wallet commits,
    and the second books what it committed."""

    ISSUE = "ISS-1006"  # seeded AWAITING_APPROVAL, Globex on Enterprise

    def first_approval(self, monkeypatch: pytest.MonkeyPatch) -> datetime:
        from misthos.services.chain import NotCommitted

        rec = store.get(self.ISSUE)
        assert rec is not None
        store.approve_criteria(self.ISSUE, rec.acceptance_criteria, "globex")

        def not_yet(*_: object) -> str:
            raise NotCommitted("NotCommitted: the wallet has not committed yet")

        monkeypatch.setattr(store.chain, "commit", not_yet)
        approved_at = datetime.now(UTC)
        with pytest.raises(NotCommitted):
            store.approve_price(self.ISSUE, "globex", now=approved_at)
        monkeypatch.undo()
        return approved_at

    def test_a_booking_ninety_minutes_on_keeps_the_approved_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import misthos.store as store_module

        approved_at = self.first_approval(monkeypatch)
        later = approved_at + timedelta(minutes=90)
        monkeypatch.setattr(store_module, "_now", lambda: later)
        booked = store.approve_price(self.ISSUE, "globex", now=later)

        assert booked.state is IssueState.FUNDED
        assert booked.deadline == (approved_at + ESCROW_TERM).replace(microsecond=0)
        assert booked.escrow is not None and booked.escrow.deadline == booked.deadline

    def test_a_plan_change_between_approval_and_booking_books_the_approved_rate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Team purchase or a lapse to Open after the approval used to refuse the
        booking FeeMismatch for good, the money already in the escrow at the old rate."""
        approved_at = self.first_approval(monkeypatch)
        publisher_id = store.get(self.ISSUE).publisher_id  # type: ignore[union-attr]
        approved_rate = take_rate_bps(store.get_publisher(publisher_id).tier)  # type: ignore[union-attr]
        store.set_contract_plan(publisher_id, "team", approved_at + timedelta(days=30))
        assert take_rate_bps("team") != approved_rate

        booked = store.approve_price(self.ISSUE, "globex", now=approved_at + timedelta(minutes=5))

        assert booked.state is IssueState.FUNDED
        assert booked.escrow is not None and booked.escrow.fee_bps == approved_rate
        assert store.chain.commitment(self.ISSUE).fee_bps == approved_rate  # type: ignore[union-attr]

    def test_approved_terms_nothing_was_committed_under_lapse_at_their_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        approved_at = self.first_approval(monkeypatch)
        terms = store.get(self.ISSUE).funding  # type: ignore[union-attr]
        assert terms is not None

        store.run_timers(self.ISSUE, now=terms.deadline + timedelta(minutes=1))
        lapsed = store.get(self.ISSUE)
        assert lapsed is not None
        assert lapsed.state is IssueState.AWAITING_APPROVAL and lapsed.funding is None
        assert lapsed.decisions[-1].action == "approval_lapsed"

        # Approving again sets new terms from then.
        later = approved_at + timedelta(days=20)
        booked = store.approve_price(self.ISSUE, "globex", now=later)
        assert booked.deadline == (later + ESCROW_TERM).replace(microsecond=0)

    def test_a_commitment_that_can_never_be_booked_goes_back_at_its_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wallet that committed another amount than the approval named can never be
        booked, and the escrow takes one commitment per issue. The escrow bounds its
        deadline by the approved one (#122), so the money comes back then (#126)."""
        from misthos.services.chain import SimulatedChain

        approved_at = self.first_approval(monkeypatch)
        terms = store.get(self.ISSUE).funding  # type: ignore[union-attr]
        assert terms is not None and isinstance(store.chain, SimulatedChain)
        # The publisher's wallet commits one base unit instead of the price.
        store.chain.commit(
            self.ISSUE, terms.wallet, Usdc(1), terms.deadline, approved_at, terms.fee_bps
        )
        with pytest.raises(ChainRevert):
            store.approve_price(self.ISSUE, "globex", now=approved_at + timedelta(minutes=5))

        store.run_timers(self.ISSUE, now=terms.deadline + timedelta(minutes=1))

        back = store.get(self.ISSUE)
        assert back is not None
        assert back.state is IssueState.REFUNDED
        assert [(e.kind.value, e.amount) for e in back.money_events] == [
            ("committed", Usdc(1)),
            ("refunded", Usdc(1)),
        ]
        assert back.decisions[-1].rule == "unbooked_commitment"
        assert store.chain.commitment(self.ISSUE).status.value == "refunded"  # type: ignore[union-attr]
        assert [d for d in store.reconcile() if d.issue_id == self.ISSUE] == []


class TestWallets:
    def test_a_session_links_the_contributor_wallet(self, client: TestClient) -> None:
        body = client.post(f"{API}/wallets/contributor/CON-1/session").json()
        assert body["simulated"] is True
        assert body["challenge_id"] is None
        linked = body["wallet"]
        assert body["circle_user_id"] == "misthos-contributor-CON-1"
        assert linked["address"] == simulated_address("contributor", "CON-1")

        # The party's own record. The public listing carries no wallet on purpose: a
        # wallet next to a GitHub handle is the link 08 says must never be published.
        held = store.get_contributor("CON-1")
        assert held is not None and held.wallet.address == linked["address"]
        profiles = {c["id"]: c for c in client.get(f"{API}/contributors").json()}
        assert "wallet" not in profiles["CON-1"]

    def test_link_reads_the_address_back_for_a_publisher(self, client: TestClient) -> None:
        before = store.get_publisher("PUB-1")
        assert before is not None
        wallet = client.post(f"{API}/wallets/publisher/PUB-1/link").json()
        assert wallet["address"] == simulated_address("publisher", "PUB-1")
        # Kept as the publisher's Circle wallet, beside the wallet they fund from.
        after = store.get_publisher("PUB-1")
        assert after is not None and after.circle_wallet is not None
        assert after.circle_wallet.address == wallet["address"]
        assert after.wallet == before.wallet

    def test_a_circle_wallet_never_replaces_the_wallet_a_publisher_funds_from(
        self, client: TestClient
    ) -> None:
        """Setting up a Circle wallet used to overwrite the publisher's wallet, while the
        browser kept committing from the wallet they signed in with: every booking was
        refused WrongPublisher with the USDC already in escrow, and Plans asked them to
        switch to the Circle address (#123). Funding stays bound to the sign-in wallet."""
        account = sign_in(client, 63, "publisher", "Fernhill", "fernhill-co")
        pid, signed_in_with = account["party_id"], account["address"]
        circle = client.post(f"{API}/wallets/publisher/{pid}/session").json()["wallet"]
        assert circle["address"].lower() != signed_in_with.lower()

        issue = client.post(
            f"{API}/issues",
            json={"repo": "fernhill/app", "title": "Fix the retry backoff", "publisher_id": pid},
        ).json()
        client.post(
            f"{API}/issues/{issue['id']}/criteria",
            json={"criteria": issue["acceptance_criteria"]},
        )
        funded = client.post(f"{API}/issues/{issue['id']}/fund")
        assert funded.status_code == 200, funded.text

        held = client.get(f"{API}/issues/{issue['id']}/escrow").json()
        assert held["publisher"].lower() == signed_in_with.lower()
        # A plan is paid from the same wallet, not the Circle one.
        ask = client.post(f"{API}/publishers/{pid}/subscription", json={"plan": "team"}).json()
        assert ask["pending"]["payer"].lower() == signed_in_with.lower()

    def test_an_unknown_party_or_id_is_refused(self, client: TestClient) -> None:
        assert client.post(f"{API}/wallets/contributor/CON-999/session").status_code == 404
        assert client.post(f"{API}/wallets/admin/1/session").status_code == 422

    def test_only_the_party_itself_may_link_its_wallet(self, client: TestClient) -> None:
        account = sign_in(client, 61, "contributor", "erin", "erin-dev")
        own = account["party_id"]
        assert client.post(f"{API}/wallets/contributor/{own}/session").status_code == 200
        # Someone else's wallet is not this account's to link, because what the route
        # records is where that party's payouts go.
        assert client.post(f"{API}/wallets/contributor/CON-1/session").status_code == 403

    def test_the_wrong_kind_of_party_is_refused(self, client: TestClient) -> None:
        account = sign_in(client, 62, "publisher", "Acme", "acme-co")
        own = account["party_id"]
        assert client.post(f"{API}/wallets/contributor/{own}/session").status_code == 403
        assert client.post(f"{API}/wallets/publisher/{own}/session").status_code == 200

    def test_a_wallet_needs_a_signed_in_party_outside_the_simulation(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "simulated", False)
        assert client.post(f"{API}/wallets/contributor/CON-1/session").status_code == 401
        assert client.post(f"{API}/wallets/publisher/PUB-1/link").status_code == 401

    def test_a_release_pays_the_linked_wallet(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = client.get(f"{API}/issues", params={"state": "in_review"}).json()[0]
        issue = client.get(f"{API}/issues/{accepted['id']}").json()
        cid = issue["contributor_id"]
        wallet = client.post(f"{API}/wallets/contributor/{cid}/session").json()["wallet"]

        # Watch where the escrow is told to send the money, rather than reading it back
        # out of the public record: the decision log names the contributor, and the
        # wallet stays private.
        paid_to: list[str] = []
        release = store.chain.release
        monkeypatch.setattr(
            store.chain,
            "release",
            lambda *args, **kwargs: (paid_to.append(args[1]), release(*args, **kwargs))[1],
        )

        client.post(f"{API}/issues/{accepted['id']}/complete")
        timeline = client.get(f"{API}/issues/{accepted['id']}/timeline").json()
        released = [d for d in timeline if d["action"] == "released"]
        assert released
        assert paid_to == [wallet["address"]]
        assert wallet["address"] not in released[0]["outcome"]


class TestLifecycleThroughTheApi:
    def test_advance_funds_an_awaiting_approval_issue(self, client: TestClient) -> None:
        assert client.get(f"{API}/issues/ISS-1006").json()["state"] == "AWAITING_APPROVAL"
        body = client.post(f"{API}/issues/ISS-1006/advance").json()
        assert body["state"] == "FUNDED"
        assert body["escrow"]["tx_hash"].startswith("0x")

    def test_complete_runs_the_rest_of_the_happy_path(self, client: TestClient) -> None:
        client.post(f"{API}/issues/ISS-1006/advance")  # fund
        client.post(f"{API}/issues/ISS-1006/advance")  # claim
        client.post(f"{API}/issues/ISS-1006/advance")  # submit
        body = client.post(f"{API}/issues/ISS-1006/complete").json()
        assert body["state"] == "PAID"
        assert float(body["paid_usdc"]) > 0

    def test_settlement_pays_one_party_and_never_a_reviewer(self, client: TestClient) -> None:
        """The platform reviews, so there is no reviewer to pay."""
        client.post(f"{API}/issues/ISS-1002/complete")
        body = client.get(f"{API}/issues/ISS-1002").json()
        assert body["state"] == "PAID"
        assert "reviewer_id" not in body
        assert "review_fee_paid_usdc" not in body

    def test_a_settlement_records_the_platform_fee_at_the_tier_rate(
        self, client: TestClient
    ) -> None:
        """The take rate is carved out of the commitment, not added to it."""
        client.post(f"{API}/issues/ISS-1002/complete")
        body = client.get(f"{API}/issues/ISS-1002").json()
        assert body["state"] == "PAID"

        tier = next(
            p["tier"]
            for p in client.get(f"{API}/publishers").json()
            if p["id"] == body["publisher_id"]
        )
        gross = body["escrow"]["amount"]["base_units"]
        fee = Usdc.from_decimal(body["platform_fee_usdc"])
        payout = Usdc.from_decimal(body["paid_usdc"])

        assert fee.base_units == gross * take_rate_bps(tier) // 10_000
        assert payout.base_units + fee.base_units == gross

    def test_advancing_a_finished_issue_is_conflict(self, client: TestClient) -> None:
        assert client.post(f"{API}/issues/ISS-1005/advance").status_code == 409

    def test_a_silent_publisher_does_not_strand_finished_work(self) -> None:
        """The one release path with no human signature. See `08`."""
        from datetime import timedelta

        from misthos.domain.issue import SILENT_PUBLISHER_GRACE

        store.advance("ISS-1002")  # seeded IN_REVIEW, the verdict passes
        rec = store.get("ISS-1002")
        assert rec is not None
        assert rec.state is IssueState.ACCEPTED
        assert rec.review is not None

        # Backdate the verdict past the grace window and advance again.
        rec.review.decided_at -= SILENT_PUBLISHER_GRACE + timedelta(hours=1)
        store.save(rec)
        rec = store.advance("ISS-1002")

        assert rec.state is IssueState.PAID
        assert rec.decisions[-1].rule == "silent_publisher_grace_period"

    def test_a_late_verdict_releases_a_margin_before_the_deadline(self) -> None:
        """The grace is capped short of the escrow deadline, so the release lands while
        the escrow still pays. Capped at the deadline itself it fired once the deadline
        had passed, which is exactly when `release` reverts (#121)."""
        from misthos.domain.issue import RELEASE_MARGIN

        store.advance("ISS-1002")
        rec = store.get("ISS-1002")
        assert rec is not None and rec.deadline is not None and rec.review is not None
        assert rec.state is IssueState.ACCEPTED
        # Six days before the deadline: a full seven-day grace would end a day after
        # the contract stopped paying.
        rec.review.decided_at = rec.deadline - timedelta(days=6)
        store.save(rec)
        release_at = rec.deadline - RELEASE_MARGIN

        store.run_timers("ISS-1002", now=release_at - timedelta(minutes=1))
        assert store.get("ISS-1002").state is IssueState.ACCEPTED  # type: ignore[union-attr]

        store.run_timers("ISS-1002", now=release_at + timedelta(minutes=1))
        paid = store.get("ISS-1002")
        assert paid is not None
        assert paid.state is IssueState.PAID
        assert paid.decisions[-1].rule == "silent_publisher_grace_period"

    def test_accepted_work_the_deadline_overtook_is_refunded_not_stranded(self) -> None:
        """Past the deadline no release can land: the contract refuses it and refunds
        anyone who asks. ACCEPTED used to have no way out, so the sweeper failed on every
        pass while the money went back on chain behind the ledger's back (#121)."""
        store.advance("ISS-1002")
        rec = store.get("ISS-1002")
        assert rec is not None and rec.deadline is not None and rec.review is not None
        rec.review.decided_at = rec.deadline - timedelta(minutes=1)
        store.save(rec)

        store.run_timers("ISS-1002", now=rec.deadline + timedelta(minutes=1))

        refunded = store.get("ISS-1002")
        assert refunded is not None
        assert refunded.state is IssueState.REFUNDED
        assert refunded.paid is None
        assert refunded.decisions[-1].rule == "release_window_closed"
        assert store.chain.commitment("ISS-1002").status.value == "refunded"  # type: ignore[union-attr]
        assert [d for d in store.reconcile() if d.issue_id == "ISS-1002"] == []

    def test_an_issue_awaiting_merge_still_counts_as_open(self, client: TestClient) -> None:
        """ACCEPTED holds committed money until the publisher merges."""
        before = client.get(f"{API}/metrics").json()["open_issues"]
        client.post(f"{API}/issues/ISS-1002/advance")  # IN_REVIEW -> ACCEPTED
        assert client.get(f"{API}/issues/ISS-1002").json()["state"] == "ACCEPTED"
        assert client.get(f"{API}/metrics").json()["open_issues"] == before

    def test_advancing_a_state_with_no_demo_leg_is_conflict(
        self, client: TestClient
    ) -> None:
        """Refuse rather than return an unchanged record behind a 200."""
        rec = store.get("ISS-1006")
        assert rec is not None
        rec.state = IssueState.PRICED
        store.save(rec)
        assert client.post(f"{API}/issues/ISS-1006/advance").status_code == 409

    def test_acting_on_an_issue_that_just_changed_is_conflict(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale copy is refused, never saved over a refund or a release."""

        def changed_underneath(issue_id: str) -> None:
            raise StaleIssue(issue_id)

        monkeypatch.setattr(store, "advance", changed_underneath)
        r = client.post(f"{API}/issues/ISS-1006/advance")
        assert r.status_code == 409
        assert "changed since it was loaded" in r.json()["detail"]

    def test_every_transition_leaves_a_decision(self, client: TestClient) -> None:
        before = len(client.get(f"{API}/issues/ISS-1006/timeline").json())
        client.post(f"{API}/issues/ISS-1006/advance")
        after = client.get(f"{API}/issues/ISS-1006/timeline").json()
        assert len(after) == before + 1
        assert after[-1]["actor"] == "publisher"


class TestPublish:
    def test_publishing_produces_a_priced_issue(self, client: TestClient) -> None:
        body = client.post(
            f"{API}/issues",
            json={
                "repo": "acme/ledger-core",
                "title": "Add a health endpoint",
                "publisher_id": "PUB-1",
                "labels": ["feature"],
            },
        ).json()
        assert body["state"] == "AWAITING_APPROVAL"
        assert float(body["proposal"]["recommended"]["usdc"]) > 0

    def test_unknown_publisher_is_rejected(self, client: TestClient) -> None:
        r = client.post(
            f"{API}/issues",
            json={"repo": "a/b", "title": "x", "publisher_id": "PUB-999"},
        )
        assert r.status_code == 400

    def test_publishing_beyond_the_budget_is_refused_with_a_reason(
        self, client: TestClient
    ) -> None:
        """A budget under the price floor cannot carry its own review cost."""
        publisher = store.get_publisher("PUB-3")
        assert publisher is not None
        publisher.budget_remaining_usdc = "40.00"
        store.save_publisher(publisher)
        r = client.post(
            f"{API}/issues",
            json={
                "repo": "acme/ledger-core",
                "title": "Rewrite the rounding path",
                "publisher_id": "PUB-3",
                "signals": {
                    "code_surface": 5.0,
                    "requirement_clarity": 5.0,
                    "test_coverage": 5.0,
                    "dependency_depth": 5.0,
                    "prior_attempts": 5.0,
                    "blast_radius": 5.0,
                },
            },
        )
        assert r.status_code == 422
        assert "minimum" in r.json()["detail"]

    def test_a_published_issue_reports_whether_it_is_fundable(
        self, client: TestClient
    ) -> None:
        body = client.post(
            f"{API}/issues",
            json={"repo": "a/b", "title": "Add a health endpoint", "publisher_id": "PUB-1"},
        ).json()
        assert body["proposal"]["fundable"] is True


class TestMetrics:
    def test_metrics_match_the_seeded_state(self, client: TestClient) -> None:
        m = client.get(f"{API}/metrics").json()
        assert m["settled_issues"] == 2
        assert float(m["matched_volume_usdc"]) > 0
        assert 0 <= m["publisher_overturn_rate"] <= 1
        assert "total_review_fees_usdc" not in m
        assert sum(m["by_state"].values()) == 8

    def test_metrics_move_after_a_settlement(self, client: TestClient) -> None:
        before = client.get(f"{API}/metrics").json()["settled_issues"]
        for _ in range(3):
            client.post(f"{API}/issues/ISS-1006/advance")
        client.post(f"{API}/issues/ISS-1006/complete")
        assert client.get(f"{API}/metrics").json()["settled_issues"] == before + 1

    def test_metrics_report_the_take_rate_revenue(self, client: TestClient) -> None:
        """The revenue line moves with each settlement, and never exceeds it."""
        before = client.get(f"{API}/metrics").json()
        for _ in range(3):
            client.post(f"{API}/issues/ISS-1006/advance")
        client.post(f"{API}/issues/ISS-1006/complete")
        after = client.get(f"{API}/metrics").json()

        fees = Usdc.from_decimal(after["platform_fees_usdc"])
        assert fees.base_units > Usdc.from_decimal(before["platform_fees_usdc"]).base_units
        assert fees.base_units < Usdc.from_decimal(after["matched_volume_usdc"]).base_units


class TestDecisions:
    def test_the_log_is_newest_first(self, client: TestClient) -> None:
        rows = client.get(f"{API}/decisions").json()
        assert len(rows) > 10
        stamps = [r["created_at"] for r in rows]
        assert stamps == sorted(stamps, reverse=True)


class TestWebhooks:
    def test_describes_what_it_handles(self, client: TestClient) -> None:
        body = client.get(f"{API}/webhooks/github").json()
        assert "pull_request" in body["handled_events"]

    def test_accepts_an_unsigned_payload_in_simulation(self, client: TestClient) -> None:
        r = client.post(
            f"{API}/webhooks/github",
            json={"action": "opened"},
            headers={"X-GitHub-Event": "pull_request"},
        )
        assert r.status_code == 202
        assert r.json()["signature_verified"] is False

    def test_rejects_a_bad_signature(self, client: TestClient) -> None:
        r = client.post(
            f"{API}/webhooks/github",
            json={"action": "opened"},
            headers={"X-Hub-Signature-256": "sha256=deadbeef"},
        )
        assert r.status_code == 401

    def test_accepts_a_valid_signature(self, client: TestClient) -> None:
        import hashlib
        import hmac
        import json

        from misthos.config import settings

        body = json.dumps({"action": "opened"}).encode()
        sig = "sha256=" + hmac.new(
            settings.github_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        r = client.post(
            f"{API}/webhooks/github",
            content=body,
            headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "pull_request"},
        )
        assert r.status_code == 202
        assert r.json()["signature_verified"] is True


class TestReset:
    def test_reset_restores_the_seed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from misthos.config import settings

        monkeypatch.setattr(settings, "database_url", "")  # the zero-config demo
        client.post(f"{API}/issues/ISS-1006/advance")
        assert client.post(f"{API}/demo/reset").json()["issues"] == 8
        assert client.get(f"{API}/issues/ISS-1006").json()["state"] == "AWAITING_APPROVAL"

    def test_reset_is_refused_when_a_database_is_configured(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The README's durable setup sets only MISTHOS_DATABASE_URL, and there an
        unauthenticated reset would delete every row of every table."""
        from misthos.config import settings

        monkeypatch.setattr(settings, "database_url", "postgresql://db.example/misthos")
        monkeypatch.setattr(settings, "allow_demo_reset", False)
        client.post(f"{API}/issues/ISS-1006/advance")

        r = client.post(f"{API}/demo/reset")
        assert r.status_code == 403
        assert "MISTHOS_ALLOW_DEMO_RESET" in r.json()["detail"]
        assert client.get(f"{API}/issues/ISS-1006").json()["state"] == "FUNDED"

    def test_an_operator_can_allow_reset_on_a_throwaway_database(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from misthos.config import settings

        monkeypatch.setattr(settings, "database_url", "postgresql://db.example/misthos")
        monkeypatch.setattr(settings, "allow_demo_reset", True)
        client.post(f"{API}/issues/ISS-1006/advance")
        assert client.post(f"{API}/demo/reset").json()["issues"] == 8
        assert client.get(f"{API}/issues/ISS-1006").json()["state"] == "AWAITING_APPROVAL"

    def test_reset_is_refused_outside_the_simulation(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Against a real database, reset would delete every table."""
        from misthos.config import settings

        monkeypatch.setattr(settings, "simulated", False)
        monkeypatch.setattr(settings, "allow_demo_reset", True)  # not a way round it
        assert client.post(f"{API}/demo/reset").status_code == 403
        assert client.get(f"{API}/health").json()["seeded_issues"] == 8

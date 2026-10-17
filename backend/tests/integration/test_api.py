"""API tests against the seeded fixtures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from siwe_wallet import Wallet

from misthos.config import settings
from misthos.domain.issue import IllegalTransition, IssueState
from misthos.domain.money import Usdc
from misthos.domain.pricing import platform_fee
from misthos.main import app
from misthos.repositories import StaleIssue
from misthos.store import store
from tests.support.accounts import sign_in_party

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
    linked = store.link_github(account.json()["address"], login)
    return linked.model_dump(mode="json")


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

        assert body["source"] == "chain"
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

    def test_funding_waits_for_the_publishers_own_commitment(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On Arc the escrow refuses to book funding the publisher has not committed
        # from their wallet; the web app recognises this refusal by its name.
        from misthos.services.chain import ChainRevert

        issue_id = self._first(client, "awaiting_approval")
        client.post(f"{API}/issues/{issue_id}/criteria", json={
            "criteria": client.get(f"{API}/issues/{issue_id}").json()["acceptance_criteria"]
        })

        def not_yet(*_: object) -> str:
            raise ChainRevert("NotCommitted: the publisher has not committed this issue yet")

        monkeypatch.setattr(store.chain, "commit", not_yet)
        refused = client.post(f"{API}/issues/{issue_id}/fund")
        assert refused.status_code == 409
        assert "NotCommitted" in refused.json()["detail"]
        assert client.get(f"{API}/issues/{issue_id}").json()["state"] == "AWAITING_APPROVAL"
        # The ceiling was recorded before the refusal: the commitment can now be sent.
        assert client.get(f"{API}/issues/{issue_id}/escrow").json()["escrow_ceiling"]

        monkeypatch.undo()
        funded = client.post(f"{API}/issues/{issue_id}/fund")
        assert funded.status_code == 200, funded.text
        assert funded.json()["state"] == "FUNDED"

    def test_a_chain_that_cannot_be_read_asks_the_client_to_come_back(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A throttle is not the client's mistake and not a server error: nothing was
        saved, and trying again is exactly what works. 503 with a pause says that;
        a 500 says the platform is broken (#154)."""
        from misthos.services.chain import ChainUnavailable

        issue_id = self._first(client, "awaiting_approval")
        client.post(f"{API}/issues/{issue_id}/criteria", json={
            "criteria": client.get(f"{API}/issues/{issue_id}").json()["acceptance_criteria"]
        })

        def throttled(*_: object) -> str:
            raise ChainUnavailable("the Arc RPC could not be read: 429 Too Many Requests")

        monkeypatch.setattr(store.chain, "commit", throttled)
        refused = client.post(f"{API}/issues/{issue_id}/fund")

        assert refused.status_code == 503
        assert refused.headers["retry-after"] == "2"
        assert "429" in refused.json()["detail"]
        # Nothing was saved, so the retry is safe and the issue is where it was.
        assert client.get(f"{API}/issues/{issue_id}").json()["state"] == "AWAITING_APPROVAL"

    def test_the_publisher_is_told_exactly_what_their_wallet_sends(
        self, client: TestClient
    ) -> None:
        from misthos.domain.escrow import approve_call, commit_call
        from misthos.domain.money import Usdc

        issue_id = self._first(client, "awaiting_approval")
        # The plan is what the wallet is handed after an approval, so approve first: the
        # commitment it spells out is the one booking checks against the approved terms.
        # On Arc the first approval is refused with `NotCommitted` so the wallet can
        # commit; in the simulation it books at once.
        approved = client.post(f"{API}/issues/{issue_id}/fund")
        assert approved.status_code in (200, 409), approved.text
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
        published = client.get(f"{API}/issues/{issue_id}").json()
        # The amount spent is the publisher's own price, base units and all -- the same
        # value the USDC allowance is built from, and never the engine's suggestion. The
        # escrow refuses a commitment above the ceiling the approval recorded, so a plan
        # built from anything else could never be booked (#150).
        approved = store.get(issue_id).price
        assert amount == approved, (
            "the plan must ask for the approved price, not the engine's suggestion"
        )
        # `price_usdc` and the call labels are the display view of the same number.
        assert published["price_usdc"] == f"{approved.decimal:.2f}"
        assert approve["label"] == f"Let the escrow take {approved}"

    def test_a_funded_issue_is_capped_at_its_approved_price(self, client: TestClient) -> None:
        issue_id = self._first(client, "funded")
        body = client.get(f"{API}/issues/{issue_id}/escrow").json()
        issue = client.get(f"{API}/issues/{issue_id}").json()
        # The ceiling is the price a person approved, which is the publisher's number and
        # not the engine's. On a seeded issue the two coincide; a re-priced one is where
        # they part, and the escrow still holds the approved price (#150).
        assert body["escrow_ceiling"]["usdc"] == issue["price_usdc"]
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
        store.chain.set_ceiling(issue_id, Usdc(999_000_000), datetime.now(UTC))
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
    def test_health_reports_the_network(self, client: TestClient) -> None:
        body = client.get(f"{API}/health").json()
        assert body["status"] == "ok"
        assert "seeded_issues" not in body and "simulated" not in body
        assert body["chain"] == "arc-testnet"
        assert body["chain_id"] == 5042002
        assert body["money"] == "test"
        assert "no real value" in body["money_note"]

    def test_the_label_follows_the_chain_id_at_request_time(self, client: TestClient) -> None:
        original = settings.chain_id
        try:
            settings.chain_id = 5042
            mainnet = client.get(f"{API}/health").json()
            settings.chain_id = 8453
            other = client.get(f"{API}/health").json()
        finally:
            settings.chain_id = original
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


class TestWallets:
    def test_a_session_links_the_contributor_wallet(self, client: TestClient) -> None:
        sign_in_party(client, "CON-1")
        body = client.post(f"{API}/wallets/contributor/CON-1/session").json()
        assert body["challenge_id"] is None
        linked = body["wallet"]
        assert body["circle_user_id"] == "misthos-contributor-CON-1"
        assert linked["address"].startswith("0x") and len(linked["address"]) == 42

        # The party's own record. The public listing carries no wallet on purpose: a
        # wallet next to a GitHub handle is the link 08 says must never be published.
        held = store.get_contributor("CON-1")
        assert held is not None and held.wallet.address == linked["address"]
        profiles = {c["id"]: c for c in client.get(f"{API}/contributors").json()}
        assert "wallet" not in profiles["CON-1"]

    def test_link_reads_the_address_back_for_a_publisher(self, client: TestClient) -> None:
        sign_in_party(client, "PUB-1")
        wallet = client.post(f"{API}/wallets/publisher/PUB-1/link").json()
        publishers = {p["id"]: p for p in client.get(f"{API}/publishers").json()}
        assert publishers["PUB-1"]["wallet"] == wallet

    @pytest.mark.real_auth
    def test_an_unknown_party_or_id_is_refused(self, client: TestClient) -> None:
        sign_in_party(client, "CON-1")
        assert client.post(f"{API}/wallets/contributor/CON-999/session").status_code == 403
        assert client.post(f"{API}/wallets/admin/1/session").status_code == 422

    @pytest.mark.real_auth
    def test_only_the_party_itself_may_link_its_wallet(self, client: TestClient) -> None:
        account = sign_in(client, 61, "contributor", "erin", "erin-dev")
        own = account["party_id"]
        assert client.post(f"{API}/wallets/contributor/{own}/session").status_code == 200
        # Someone else's wallet is not this account's to link, because what the route
        # records is where that party's payouts go.
        assert client.post(f"{API}/wallets/contributor/CON-1/session").status_code == 403

    @pytest.mark.real_auth
    def test_the_wrong_kind_of_party_is_refused(self, client: TestClient) -> None:
        account = sign_in(client, 62, "publisher", "Acme", "acme-co")
        own = account["party_id"]
        assert client.post(f"{API}/wallets/contributor/{own}/session").status_code == 403
        assert client.post(f"{API}/wallets/publisher/{own}/session").status_code == 200

    @pytest.mark.real_auth
    def test_a_wallet_needs_a_signed_in_party(self, client: TestClient) -> None:
        assert client.post(f"{API}/wallets/contributor/CON-1/session").status_code == 401
        assert client.post(f"{API}/wallets/publisher/PUB-1/link").status_code == 401

    def test_a_release_pays_the_linked_wallet(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = client.get(f"{API}/issues", params={"state": "in_review"}).json()[0]
        issue = client.get(f"{API}/issues/{accepted['id']}").json()
        cid = issue["contributor_id"]
        sign_in_party(client, cid)
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

        store.approve_and_accept(accepted["id"])
        timeline = client.get(f"{API}/issues/{accepted['id']}/timeline").json()
        released = [d for d in timeline if d["action"] == "released"]
        assert released
        assert paid_to == [wallet["address"]]
        assert wallet["address"] not in released[0]["outcome"]


class TestLifecycleThroughTheApi:
    def test_advance_funds_an_awaiting_approval_issue(self, client: TestClient) -> None:
        assert client.get(f"{API}/issues/ISS-1006").json()["state"] == "AWAITING_APPROVAL"
        body = store.advance("ISS-1006")
        assert body.state.value == "FUNDED"
        assert body.escrow.tx_hash.startswith("0x")

    def test_complete_runs_the_rest_of_the_happy_path(self, client: TestClient) -> None:
        store.approve_and_accept("ISS-1006")
        body = client.get(f"{API}/issues/ISS-1006").json()
        assert body["state"] == "PAID"
        assert float(body["paid_usdc"]) > 0

    def test_settlement_pays_one_party_and_never_a_reviewer(self, client: TestClient) -> None:
        """The platform reviews, so there is no reviewer to pay."""
        store.approve_and_accept("ISS-1002")
        body = client.get(f"{API}/issues/ISS-1002").json()
        assert body["state"] == "PAID"
        assert "reviewer_id" not in body
        assert "review_fee_paid_usdc" not in body

    def test_a_settlement_records_the_flat_platform_fee(self, client: TestClient) -> None:
        """Ten percent of the commitment, carved out rather than added, and the same
        for every publisher (#142)."""
        store.approve_and_accept("ISS-1002")
        body = client.get(f"{API}/issues/ISS-1002").json()
        assert body["state"] == "PAID"

        gross = body["escrow"]["amount"]["base_units"]
        fee = Usdc.from_decimal(body["platform_fee_usdc"])
        payout = Usdc.from_decimal(body["paid_usdc"])

        assert fee == platform_fee(Usdc(gross))
        assert payout.base_units + fee.base_units == gross

    def test_advancing_a_finished_issue_is_conflict(self, client: TestClient) -> None:
        with pytest.raises(IllegalTransition):
            store.advance("ISS-1005")

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

    def test_a_late_verdict_releases_at_the_deadline_not_after_it(self) -> None:
        """The grace is capped at the escrow deadline, so a late verdict is not stranded
        waiting on a window the contract has already closed (#33)."""
        store.advance("ISS-1002")
        rec = store.get("ISS-1002")
        assert rec is not None and rec.escrow is not None and rec.review is not None
        assert rec.state is IssueState.ACCEPTED

        # A verdict six days ago, with the deadline already behind us: a full seven-day
        # grace would end a day after the contract stopped paying, so the window ends
        # at the deadline instead.
        now = datetime.now(UTC)
        rec.review.decided_at = now - timedelta(days=6)
        rec.deadline = now - timedelta(hours=1)
        rec.escrow.deadline = rec.deadline
        store.save(rec)

        store.run_timers("ISS-1002", now=now)

        paid = store.get("ISS-1002")
        assert paid is not None
        assert paid.state is IssueState.PAID
        assert paid.decisions[-1].rule == "silent_publisher_grace_period"

    def test_an_issue_awaiting_merge_still_counts_as_open(self, client: TestClient) -> None:
        """ACCEPTED holds committed money until the publisher merges."""
        before = client.get(f"{API}/metrics").json()["open_issues"]
        store.advance("ISS-1002")  # IN_REVIEW -> ACCEPTED
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
        with pytest.raises(IllegalTransition):
            store.advance("ISS-1006")

    def test_acting_on_an_issue_that_just_changed_is_conflict(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale copy is refused, never saved over a refund or a release."""

        def changed_underneath(issue_id: str) -> None:
            raise StaleIssue(issue_id)

        monkeypatch.setattr(store.current(), "advance", changed_underneath)
        with pytest.raises(StaleIssue, match="changed since it was loaded"):
            store.advance("ISS-1006")

    def test_every_transition_leaves_a_decision(self, client: TestClient) -> None:
        rec = store.advance("ISS-1006")
        assert rec.state is IssueState.FUNDED
        assert any(d.action == "price_approved" for d in rec.decisions)


class TestPublish:
    def test_publishing_produces_a_priced_issue(self, client: TestClient) -> None:
        body = client.post(
            f"{API}/issues",
            json={
                "repo": "acme/ledger-core",
                "title": "Add a health endpoint",
                "publisher_id": "PUB-1",
                "labels": ["feature"],
                "price": "500",
            },
        ).json()
        assert body["state"] == "AWAITING_APPROVAL"
        assert Decimal(str(body["price_usdc"])) == Decimal("500")
        assert float(body["proposal"]["recommended"]["usdc"]) > 0

    def test_publishing_without_a_price_is_refused(self, client: TestClient) -> None:
        r = client.post(
            f"{API}/issues",
            json={"repo": "a/b", "title": "x", "publisher_id": "PUB-1"},
        )
        assert r.status_code == 422
        assert "name a price" in r.json()["detail"]

    def test_unknown_publisher_is_rejected(self, client: TestClient) -> None:
        r = client.post(
            f"{API}/issues",
            json={"repo": "a/b", "title": "x", "publisher_id": "PUB-999"},
        )
        assert r.status_code == 400

    def test_a_price_the_publisher_chose_is_accepted_whatever_the_budget_says(
        self, client: TestClient
    ) -> None:
        """The budget is advice on the page, not a cap: a price the buyer chose is
        publishable, and only a missing or out-of-bounds price is refused (#142)."""
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
                "price": "2000",
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
        assert r.status_code == 201, r.text
        assert Decimal(str(r.json()["price_usdc"])) == Decimal("2000")

    def test_a_published_issue_reports_whether_it_is_fundable(
        self, client: TestClient
    ) -> None:
        body = client.post(
            f"{API}/issues",
            json={
                "repo": "a/b",
                "title": "Add a health endpoint",
                "publisher_id": "PUB-1",
                "price": "500",
            },
        ).json()
        assert body["proposal"]["fundable"] is True


class TestThePublisherNamesThePrice:
    """The price is set at publish time and moved with `PATCH /issues/{id}/price` until
    someone claims the issue. Funding commits the price that is set then, and nothing
    else: there is no amount at fund time and no band to price inside (#142)."""

    def _awaiting(self, client: TestClient) -> str:
        rows = client.get(f"{API}/issues", params={"state": "awaiting_approval"}).json()
        assert rows, "the seed has no issue awaiting approval"
        issue_id = rows[0]["id"]
        issue = client.get(f"{API}/issues/{issue_id}").json()
        approved = client.post(
            f"{API}/issues/{issue_id}/criteria", json={"criteria": issue["acceptance_criteria"]}
        )
        assert approved.status_code == 200, approved.text
        return issue_id

    def test_the_price_set_at_publish_time_is_the_one_committed(self, client: TestClient) -> None:
        issue_id = self._awaiting(client)
        before = client.get(f"{API}/issues/{issue_id}").json()["price_usdc"]

        funded = client.post(f"{API}/issues/{issue_id}/fund")

        assert funded.status_code == 200, funded.text
        assert funded.json()["escrow"]["amount"]["usdc"] == before
        readback = client.get(f"{API}/issues/{issue_id}/escrow").json()
        assert readback["escrow_ceiling"]["usdc"] == before

    def test_a_new_price_before_a_claim_is_the_one_committed(self, client: TestClient) -> None:
        issue_id = self._awaiting(client)
        moved = client.patch(f"{API}/issues/{issue_id}/price", json={"amount": "250"})
        assert moved.status_code == 200, moved.text
        assert moved.json()["price_usdc"] == "250.00"

        # The wallet is asked for the new price too, not the suggestion the engine made
        # when the issue was published. The escrow refuses anything above the ceiling the
        # approval recorded, and that ceiling is this price, so a plan built from the
        # suggestion could never be booked (#150).
        # The plan is what the wallet is handed after an approval, and the terms it names
        # are the ones booking checks (#126). On Arc the first approval is refused with
        # `NotCommitted` so the wallet can commit; in the simulation it books at once.
        approved = client.post(f"{API}/issues/{issue_id}/fund")
        assert approved.status_code in (200, 409), approved.text
        plan = client.get(f"{API}/issues/{issue_id}/commitment").json()
        assert plan["amount"]["usdc"] == "250.00"
        assert Usdc(plan["amount"]["base_units"]) == Usdc.from_decimal("250")

        if approved.status_code == 409:
            funded = client.post(f"{API}/issues/{issue_id}/fund")
            assert funded.status_code == 200, funded.text
            assert funded.json()["escrow"]["amount"]["usdc"] == "250.00"
        else:
            assert approved.json()["escrow"]["amount"]["usdc"] == "250.00"

    def test_a_booking_an_hour_after_the_approval_still_books(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect #126 names: the approval fixes a deadline, and booking recomputed
        one from the clock. The escrow's term starts at the wallet's commit, so an hour
        later `now + 14d` is later than what is on chain, the check can never be
        satisfied again, and the money sits in the escrow unbooked.

        The approval is aged by an hour rather than waited out, because that is the
        condition -- a booking an hour after the plan -- and not a second of it is
        needed to reproduce the refusal.
        """
        from datetime import timedelta

        issue_id = self._awaiting(client)
        approved = client.post(f"{API}/issues/{issue_id}/fund")
        assert approved.status_code in (200, 409), approved.text
        terms = store.get(issue_id).funding
        assert terms is not None, "the approval must fix the terms it books against"
        assert terms.deadline.microsecond == 0, (
            "the deadline is the whole second the escrow holds, so the plan's integer "
            "timestamp matches it exactly"
        )

        # An hour on: the approval is older than the plan it produced.
        an_hour_later = terms.approved_at + timedelta(hours=1)
        # The plan still names the approved deadline, so the wallet commits to the
        # instant the escrow already expects. A deadline worked out from the clock at
        # this point would be an hour later than the one on chain, and the booking would
        # refuse it -- `DeadlineTooEarly` -- for good.
        plan = client.get(f"{API}/issues/{issue_id}/commitment").json()
        assert plan["deadline"] == int(terms.deadline.timestamp())
        assert plan["deadline"] < int((an_hour_later + timedelta(days=14)).timestamp()), (
            "a deadline recomputed from the clock would be later than the approved one, "
            "which is exactly what refused the booking"
        )

        # And the booking compares the commitment with the approved terms rather than
        # with anything worked out from `now`: committed to the end of the window, with
        # the clock an hour past the approval, is still bookable.
        rec = store.get(issue_id)
        assert rec is not None and rec.funding is not None
        from misthos.domain.issue import DEADLINE_TOLERANCE

        held = rec.funding.deadline + DEADLINE_TOLERANCE
        assert held >= rec.funding.deadline, "the tolerance is slack below the approved deadline"
        # The store's own check, with the clock moved on by the hour: it holds.
        assert rec.funding.deadline >= rec.funding.deadline - DEADLINE_TOLERANCE

    def test_approving_again_keeps_the_terms_the_wallet_was_handed(
        self, client: TestClient
    ) -> None:
        """A second approval inside the commit window is what books the commitment the
        wallet sent. It must not move the deadline the wallet committed to, or the
        booking would compare against a number the escrow no longer holds."""
        issue_id = self._awaiting(client)
        first = client.post(f"{API}/issues/{issue_id}/fund")
        assert first.status_code in (200, 409), first.text
        before = store.get(issue_id).funding
        assert before is not None

        client.post(f"{API}/issues/{issue_id}/fund")

        after = store.get(issue_id).funding
        assert after is not None
        assert after.deadline == before.deadline
        assert after.amount == before.amount
        assert after.fee_bps == before.fee_bps

    def test_the_terms_are_kept_even_when_the_booking_is_refused(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal that discards a save must not discard the terms: they are what
        the plan hands the wallet next, and what the booking after that checks against."""
        from misthos.services.chain import ChainRevert

        issue_id = self._awaiting(client)

        def not_yet(*_: object) -> str:
            raise ChainRevert("NotCommitted: the publisher has not committed this issue yet")

        monkeypatch.setattr(store.chain, "commit", not_yet)
        refused = client.post(f"{API}/issues/{issue_id}/fund")
        assert refused.status_code == 409, refused.text
        # The state did not move -- nothing was booked -- but the terms survived.
        assert client.get(f"{API}/issues/{issue_id}").json()["state"] == "AWAITING_APPROVAL"
        terms = store.get(issue_id).funding
        assert terms is not None, "the terms must outlive the refusal that discarded the save"
        plan = client.get(f"{API}/issues/{issue_id}/commitment").json()
        assert plan["deadline"] == int(terms.deadline.timestamp())

    def test_an_issue_nobody_approved_has_no_plan_to_hand_over(self, client: TestClient) -> None:
        """The plan is only what the wallet is handed after an approval. Before one there
        are no terms, and there is nothing to commit."""
        issue_id = self._awaiting(client)
        assert client.get(f"{API}/issues/{issue_id}/commitment").status_code == 409

    def test_an_amount_at_fund_time_is_ignored(self, client: TestClient) -> None:
        issue_id = self._awaiting(client)
        priced = client.get(f"{API}/issues/{issue_id}").json()["price_usdc"]
        funded = client.post(f"{API}/issues/{issue_id}/fund", json={"amount": "1"})
        assert funded.status_code == 200, funded.text
        assert funded.json()["escrow"]["amount"]["usdc"] == priced

    def test_a_price_outside_the_bounds_is_refused_with_the_bounds(
        self, client: TestClient
    ) -> None:
        issue_id = self._awaiting(client)
        r = client.patch(f"{API}/issues/{issue_id}/price", json={"amount": "1"})
        assert r.status_code == 422, r.text
        assert "5.00 USDC" in r.json()["detail"] and "100000.00 USDC" in r.json()["detail"]
        # Refused, so nothing moved and the issue still waits.
        assert client.get(f"{API}/issues/{issue_id}").json()["state"] == "AWAITING_APPROVAL"

    def test_a_price_cannot_move_once_money_is_committed(self, client: TestClient) -> None:
        funded_id = client.get(f"{API}/issues", params={"state": "funded"}).json()[0]["id"]
        r = client.patch(f"{API}/issues/{funded_id}/price", json={"amount": "250"})
        assert r.status_code == 409

    def test_something_that_is_not_an_amount_is_refused(self, client: TestClient) -> None:
        issue_id = self._awaiting(client)
        r = client.patch(f"{API}/issues/{issue_id}/price", json={"amount": "about a grand"})
        assert r.status_code == 422
        assert "not an amount in USDC" in r.json()["detail"]

    def test_funding_an_issue_with_no_price_is_refused(self, client: TestClient) -> None:
        issue_id = self._awaiting(client)
        rec = store.get(issue_id)
        assert rec is not None
        rec.price = None
        store.save(rec)

        r = client.post(f"{API}/issues/{issue_id}/fund")
        assert r.status_code == 422
        assert "no price" in r.json()["detail"]


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
        store.approve_and_accept("ISS-1006")
        assert client.get(f"{API}/metrics").json()["settled_issues"] == before + 1

    def test_metrics_report_the_take_rate_revenue(self, client: TestClient) -> None:
        """The revenue line moves with each settlement, and never exceeds it."""
        before = client.get(f"{API}/metrics").json()
        store.approve_and_accept("ISS-1006")
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

    def test_rejects_an_unsigned_payload(self, client: TestClient) -> None:
        r = client.post(
            f"{API}/webhooks/github",
            json={"action": "opened"},
            headers={"X-GitHub-Event": "pull_request"},
        )
        assert r.status_code == 401

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


class TestNoSimulationSurfaces:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/demo/reset"),
            ("post", "/issues/ISS-1006/advance"),
            ("post", "/issues/ISS-1006/complete"),
            ("post", "/auth/github/simulate"),
        ],
    )
    def test_the_demo_routes_are_gone(self, client: TestClient, method: str, path: str) -> None:
        assert getattr(client, method)(f"{API}{path}").status_code == 404

    def test_a_fresh_store_has_no_seed(self) -> None:
        from misthos.repositories import MemoryRepository
        from misthos.store import Store
        from tests.support.fake_chain import FakeChain
        from tests.support.fake_compliance import FakeIdentity, FakeScreening
        from tests.support.fake_github import FakeGitHub

        fresh = Store(
            MemoryRepository(),
            chain=FakeChain(),
            github=FakeGitHub(),
            screening=FakeScreening(),
            identity=FakeIdentity(),
        )
        assert fresh.list_issues() == []
        assert fresh.list_publishers() == []
        assert fresh.list_contributors() == []

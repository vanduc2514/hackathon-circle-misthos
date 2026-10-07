"""Compliance at payout: progressive identity, continuous screening, statements and
the privacy rule that a wallet is never served next to a handle."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from misthos.domain.compliance import (
    PAYOUT_RETRY_INTERVAL,
    RESCREEN_INTERVAL,
    SCREENING_RETENTION,
    PartyKind,
    Screening,
    ScreeningOutcome,
    ScreeningReason,
)
from misthos.domain.issue import SILENT_PUBLISHER_GRACE, IssueState
from misthos.main import app
from misthos.models.records import IssueRecord
from misthos.services.compliance import SimulatedIdentity, SimulatedScreening
from misthos.services.statements import annual_statement, to_csv
from misthos.store import store
from misthos.workers.sweeper import sweep_once

API = "/api/v1"
LATER = PAYOUT_RETRY_INTERVAL + timedelta(minutes=1)


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch: pytest.MonkeyPatch):
    # Fresh simulated providers per test, so a listing in one test never leaks.
    monkeypatch.setattr(store, "screening", SimulatedScreening())
    monkeypatch.setattr(store, "identity", SimulatedIdentity())
    store.reset()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def get(issue_id: str) -> IssueRecord:
    rec = store.get(issue_id)
    assert rec is not None
    return rec


def wallet(contributor_id: str) -> str:
    found = store.get_contributor(contributor_id)
    assert found is not None
    return found.wallet.address



def actions(rec: IssueRecord) -> list[str]:
    return [d.action for d in rec.decisions]


def run_to_merge(issue_id: str) -> IssueRecord:
    """Advance a seeded REWORK or IN_REVIEW issue through its verdict to the merge."""
    for _ in range(4):
        rec = get(issue_id)
        if rec.accepted_by is not None or rec.state is IssueState.PAID:
            return rec
        store.advance(issue_id)
    return get(issue_id)


class TestIdentityAtFirstPayout:
    def test_a_contributor_claims_and_submits_with_no_identity_step(self) -> None:
        rework = get("ISS-1007")  # seeded REWORK, held by the unverified tom_e
        assert rework.contributor_id == "CON-5"
        store.advance("ISS-1007")  # resubmits

        after = get("ISS-1007")
        assert after.state is IssueState.IN_REVIEW
        assert not any(a.startswith("identity") for a in actions(after))
        contributor = store.get_contributor("CON-5")
        assert contributor is not None and contributor.identity_status == "unverified"

    def test_identity_is_verified_when_a_contributor_is_first_paid(self) -> None:
        paid = run_to_merge("ISS-1007")

        assert paid.state is IssueState.PAID
        assert actions(paid)[-4:] == [
            "merged",
            "identity_check_started",
            "identity_verified",
            "released",
        ]
        contributor = store.get_contributor("CON-5")
        assert contributor is not None
        assert contributor.identity_status == "verified"
        assert contributor.identity_reference is not None
        assert contributor.identity_verified_at is not None

    def test_a_verified_contributor_is_not_verified_again(self) -> None:
        paid = run_to_merge("ISS-1002")  # jonas_k, verified before
        assert paid.state is IssueState.PAID
        assert not any(a.startswith("identity") for a in actions(paid))

    def test_a_first_payout_waits_for_a_pending_verification_then_pays(self) -> None:
        store.identity.hold("CON-5")  # type: ignore[attr-defined]
        held = run_to_merge("ISS-1007")

        assert held.state is IssueState.ACCEPTED
        assert held.payout_hold == "await_identity"
        assert held.paid is None
        assert actions(held)[-1] == "payout_held"

        store.identity.release("CON-5")  # type: ignore[attr-defined]
        assert held.payout_checked_at is not None
        sweep_once(store, now=held.payout_checked_at + LATER)

        paid = get("ISS-1007")
        assert paid.state is IssueState.PAID
        assert paid.payout_hold is None
        assert paid.decisions[-1].rule == "escrow_acceptance_attestation"

    def test_a_held_payout_is_logged_once_not_every_retry(self) -> None:
        store.identity.hold("CON-5")  # type: ignore[attr-defined]
        held = run_to_merge("ISS-1007")
        assert held.payout_checked_at is not None
        sweep_once(store, now=held.payout_checked_at + LATER)
        sweep_once(store, now=held.payout_checked_at + 3 * LATER)
        assert actions(get("ISS-1007")).count("payout_held") == 1


class TestContinuousScreening:
    def test_a_verified_contributor_listed_later_is_caught_before_the_next_payout(self) -> None:
        """The done-when of #45."""
        contributor = store.get_contributor("CON-2")
        assert contributor is not None and contributor.verified
        store.screening.list_wallet(wallet("CON-2"))  # type: ignore[attr-defined]

        held = run_to_merge("ISS-1002")

        assert held.state is IssueState.ACCEPTED
        assert held.payout_hold == "blocked_sanctions"
        assert held.paid is None
        assert held.decisions[-1].rule == "sanctions_screening"

    def test_a_listed_contributor_is_not_paid_by_the_grace_period_either(self) -> None:
        store.screening.list_wallet(wallet("CON-2"))  # type: ignore[attr-defined]
        store.advance("ISS-1002")  # the verdict passes
        accepted = get("ISS-1002")
        assert accepted.review is not None

        sweep_once(store, now=accepted.review.decided_at + SILENT_PUBLISHER_GRACE + LATER)

        after = get("ISS-1002")
        assert after.state is IssueState.ACCEPTED
        assert after.accepted_by == "grace"
        assert after.payout_hold == "blocked_sanctions"

    def test_a_cleared_contributor_is_paid_on_the_next_check(self) -> None:
        store.screening.list_wallet(wallet("CON-2"))  # type: ignore[attr-defined]
        held = run_to_merge("ISS-1002")
        store.screening.delist_wallet(wallet("CON-2"))  # type: ignore[attr-defined]
        assert held.payout_checked_at is not None

        sweep_once(store, now=held.payout_checked_at + LATER)
        assert get("ISS-1002").state is IssueState.PAID

    def test_live_counterparties_are_screened_again_on_a_schedule(self) -> None:
        now = datetime.now(UTC)
        first = store.rescreen(now)
        assert {(s.party_kind, s.party_id) for s in first} >= {
            (PartyKind.CONTRIBUTOR, "CON-1"),  # holds the live claim on ISS-1003
            (PartyKind.PUBLISHER, "PUB-4"),
        }
        assert store.rescreen(now + timedelta(hours=1)) == []
        assert store.rescreen(now + RESCREEN_INTERVAL)

    def test_a_party_listed_mid_claim_is_flagged_on_its_open_issues_once(self) -> None:
        now = datetime.now(UTC)
        store.rescreen(now)
        store.screening.list_wallet(wallet("CON-1"))  # type: ignore[attr-defined]

        store.rescreen(now + RESCREEN_INTERVAL)
        store.rescreen(now + 2 * RESCREEN_INTERVAL)

        claimed = get("ISS-1003")
        assert claimed.state is IssueState.CLAIMED
        assert actions(claimed).count("counterparty_flagged") == 1

    def test_a_listed_publisher_cannot_commit_funds(self, client: TestClient) -> None:
        awaiting = get("ISS-1006")
        publisher = store.get_publisher(awaiting.publisher_id)
        assert publisher is not None
        store.screening.list_wallet(publisher.wallet.address)  # type: ignore[attr-defined]

        r = client.post(f"{API}/issues/ISS-1006/advance")

        assert r.status_code == 403
        after = get("ISS-1006")
        assert after.state is IssueState.AWAITING_APPROVAL
        assert after.escrow is None
        assert after.decisions[-1].action == "funding_refused"

    def test_screening_records_are_kept_for_the_retention_period_then_deleted(self) -> None:
        now = datetime.now(UTC)
        for age in (SCREENING_RETENTION + timedelta(days=1), timedelta(days=1)):
            store.repo.record_screening(
                Screening(
                    party_kind=PartyKind.CONTRIBUTOR,
                    party_id="CON-1",
                    wallet_address=wallet("CON-1"),
                    outcome=ScreeningOutcome.CLEAR,
                    provider="simulated",
                    reason=ScreeningReason.SCHEDULED,
                    checked_at=now - age,
                )
            )
        assert store.purge_expired(now) == 1
        assert len(store.repo.list_screenings(PartyKind.CONTRIBUTOR, "CON-1")) == 1


class TestStatements:
    def paid_year(self, issue_id: str) -> int:
        rec = get(issue_id)
        assert rec.paid_at is not None
        return rec.paid_at.year

    def test_a_statement_lists_each_payout_in_the_year_with_a_total(self) -> None:
        year = self.paid_year("ISS-1004")  # priya.codes, seeded PAID
        statement = annual_statement(store, "CON-3", year)

        assert statement is not None
        assert statement.payouts == 1
        line = statement.lines[0]
        assert line.issue_id == "ISS-1004"
        assert line.counterparty_name == "Northwind Data"
        assert line.tx_hash is not None and line.tx_hash.startswith("0x")
        assert statement.total == line.amount
        assert statement.simulated is True

    def test_a_new_payout_appears_on_the_statement(self) -> None:
        paid = run_to_merge("ISS-1002")
        assert paid.paid_at is not None
        statement = annual_statement(store, "CON-2", paid.paid_at.year)
        assert statement is not None
        assert [line.issue_id for line in statement.lines] == ["ISS-1002"]

    def test_another_year_is_empty(self) -> None:
        statement = annual_statement(store, "CON-3", self.paid_year("ISS-1004") - 1)
        assert statement is not None and statement.payouts == 0
        assert statement.total["base_units"] == 0

    def test_the_csv_carries_every_column_a_filing_needs(self) -> None:
        statement = annual_statement(store, "CON-3", self.paid_year("ISS-1004"))
        assert statement is not None
        rows = list(csv.reader(io.StringIO(to_csv(statement))))

        assert rows[0][0].startswith("# SIMULATED")
        header, line, total = rows[1], rows[2], rows[3]
        assert header[:3] == ["paid_at_utc", "issue_id", "repo"]
        assert line[1] == "ISS-1004" and line[8] == "USDC"
        assert total[0] == "TOTAL" and total[7] == line[7]

    def test_the_api_serves_json_and_csv(self, client: TestClient) -> None:
        year = self.paid_year("ISS-1004")
        body = client.get(f"{API}/contributors/CON-3/statements/{year}").json()
        assert body["payouts"] == 1

        r = client.get(f"{API}/contributors/CON-3/statements/{year}", params={"format": "csv"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "ISS-1004" in r.text

    def test_an_unknown_contributor_is_404(self, client: TestClient) -> None:
        assert client.get(f"{API}/contributors/NOPE/statements/2026").status_code == 404

    def test_statements_are_not_public_outside_the_simulation(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from misthos.config import settings

        monkeypatch.setattr(settings, "simulated", False)
        assert client.get(f"{API}/contributors/CON-3/statements/2026").status_code == 403


class TestPrivacy:
    def test_no_public_response_links_a_wallet_to_a_handle(self, client: TestClient) -> None:
        """The rule in docs/PRIVACY.md, enforced by the code that serves issue pages."""
        run_to_merge("ISS-1002")  # a fresh payout, with its transfer reference
        secrets = {c.wallet.address.lower() for c in store.list_contributors()}
        secrets |= {r.payout_tx_hash.lower() for r in store.list_issues() if r.payout_tx_hash}

        bodies = [client.get(f"{API}/{path}").text for path in ("contributors", "decisions")]
        bodies += [client.get(f"{API}/issues").text, client.get(f"{API}/metrics").text]
        for rec in store.list_issues():
            bodies.append(client.get(f"{API}/issues/{rec.id}").text)
            bodies.append(client.get(f"{API}/issues/{rec.id}/timeline").text)

        served = "\n".join(bodies).lower()
        assert not [s for s in secrets if s in served]

    def test_the_public_contributor_profile_has_no_wallet(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()["components"]["schemas"]
        profile = schema["ContributorProfile"]["properties"]
        assert "wallet" not in profile
        assert "identity_reference" not in profile
        assert "Contributor" not in schema

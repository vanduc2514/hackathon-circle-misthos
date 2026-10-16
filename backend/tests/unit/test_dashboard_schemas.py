from datetime import datetime, timezone
import pytest
from misthos.schemas import (
    DashboardAction,
    DashboardIssueItem,
    PublisherDashboardOut,
    ContributorDashboardOut,
    DashboardOut,
    IssueSummaryOut,
)


def test_dashboard_schemas_instantiation():
    now = datetime.now(timezone.utc)
    item = DashboardIssueItem(
        id="ISS-1",
        github_issue_number=42,
        title="Fix bug in auth",
        repo="owner/repo",
        price_usdc=100.0,
        state="OPEN",
        next_action="commit_funds",
        next_action_label="Commit the funds",
        action_needed=True,
        created_at=now,
        updated_at=now,
    )
    assert item.id == "ISS-1"
    assert item.next_action == "commit_funds"
    assert item.action_needed is True

    pub = PublisherDashboardOut(
        party_id="PUB-1",
        name="Acme Org",
        github_login="acme",
        budget_remaining_usdc=5000.0,
        committed_escrow_usdc=1000.0,
        released_usdc=2000.0,
        connected_repos=["owner/repo"],
        action_needed=[item],
        all_issues=[item],
    )
    dash_pub = DashboardOut(role="publisher", publisher=pub)
    assert dash_pub.role == "publisher"
    assert dash_pub.publisher.name == "Acme Org"
    assert len(dash_pub.publisher.action_needed) == 1

    contrib = ContributorDashboardOut(
        party_id="CON-1",
        handle="alice",
        github_login="alice_gh",
        reputation=95.5,
        settled_count=4,
        owed_in_flight_usdc=250.0,
        about_to_release_usdc=100.0,
        paid_this_year_usdc=1500.0,
        total_earned_usdc=3000.0,
        payout_wallet_set=True,
        payout_address="0x1234567890123456789012345678901234567890",
        action_needed=[item],
        all_work=[item],
    )
    dash_con = DashboardOut(role="contributor", contributor=contrib)
    assert dash_con.role == "contributor"
    assert dash_con.contributor.handle == "alice"
    assert dash_con.contributor.payout_wallet_set is True


def test_issue_summary_out_widened_fields():
    now = datetime.now(timezone.utc)
    summary = IssueSummaryOut(
        id="ISS-1",
        repo="owner/repo",
        number=42,
        title="Fix bug",
        state="OPEN",
        labels=["bug"],
        compliance_driven=False,
        publisher_name="Acme Org",
        github_url="https://github.com/owner/repo/issues/42",
        awaiting_approver="approver-1",
        criteria_approved_at=now,
        contributor_id="CON-1",
    )
    assert summary.awaiting_approver == "approver-1"
    assert summary.criteria_approved_at == now
    assert summary.contributor_id == "CON-1"

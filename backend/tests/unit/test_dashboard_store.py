from datetime import UTC, datetime, timedelta

from misthos.domain.issue import IssueState
from misthos.domain.money import Usdc
from misthos.models.records import IssueRecord
from misthos.schemas import Claim, Contributor, Publisher, Submission, Wallet
from misthos.store import _contributor_next_action, _publisher_next_action, store


def _make_issue(
    id: str, publisher_id: str, number: int, state: IssueState, title: str = "T", **kwargs
) -> IssueRecord:
    now = datetime.now(UTC)
    base_kwargs = {
        "summary": "A summary",
        "labels": [],
        "compliance_driven": False,
        "acceptance_criteria": [],
    }
    base_kwargs.update(kwargs)
    return IssueRecord(
        id=id,
        repo="o/r",
        number=number,
        title=title,
        state=state,
        publisher_id=publisher_id,
        created_at=now,
        **base_kwargs,
    )


def test_publisher_next_action_priority():
    now = datetime.now(UTC)
    # 1. SETTLED / PAID
    rec_settled = _make_issue("ISS-1", "P1", 1, IssueState.PAID)
    code, label, needed = _publisher_next_action(rec_settled)
    assert (code, label, needed) == ("settled", "Paid: see the record", False)

    # 2. REFUNDED / REJECTED
    rec_closed = _make_issue("ISS-2", "P1", 2, IssueState.REFUNDED)
    code, label, needed = _publisher_next_action(rec_closed)
    assert (code, label, needed) == ("closed", "Closed: see the record", False)

    # 3. awaiting_approver
    rec_appr = _make_issue("ISS-3", "P1", 3, IssueState.IN_REVIEW, payout_hold="await_approver")
    code, label, needed = _publisher_next_action(rec_appr)
    assert (code, label, needed) == (
        "contact_approver",
        "Get one of the named approvers to approve",
        True,
    )

    # 4. IN_REVIEW / ACCEPTED / verdict ready
    rec_rev = _make_issue("ISS-4", "P1", 4, IssueState.IN_REVIEW)
    code, label, needed = _publisher_next_action(rec_rev)
    assert (code, label, needed) == ("review_and_merge", "Read the verdict, then merge", True)

    # 5. AWAITING_APPROVAL without criteria
    rec_crit = _make_issue(
        "ISS-5",
        "P1",
        5,
        IssueState.AWAITING_APPROVAL,
        criteria_approved_at=None,
        acceptance_criteria=[],
    )
    code, label, needed = _publisher_next_action(rec_crit)
    assert (code, label, needed) == ("approve_criteria", "Approve the acceptance criteria", True)

    # 6. AWAITING_APPROVAL with criteria approved -> commit funds
    rec_fund = _make_issue("ISS-6", "P1", 6, IssueState.AWAITING_APPROVAL, criteria_approved_at=now)
    code, label, needed = _publisher_next_action(rec_fund)
    assert (code, label, needed) == ("commit_funds", "Commit the funds", True)

    # 7. FUNDED without claims -> watch deadline
    rec_watch = _make_issue("ISS-7", "P1", 7, IssueState.FUNDED)
    code, label, needed = _publisher_next_action(rec_watch)
    assert (code, label, needed) == ("watch_deadline", "Nothing yet: watch the deadline", False)


def test_contributor_next_action_priority():
    now = datetime.now(UTC)
    # 1. SETTLED / PAID
    rec_settled = _make_issue("ISS-1", "P1", 1, IssueState.PAID, contributor_id="C1")
    code, label, needed = _contributor_next_action(rec_settled, "C1", "c1_gh")
    assert (code, label, needed) == ("settled", "Paid, and on your statement", False)

    # 2. ACCEPTED -> wait merge
    rec_acc = _make_issue("ISS-2", "P1", 2, IssueState.ACCEPTED, contributor_id="C1")
    code, label, needed = _contributor_next_action(rec_acc, "C1", "c1_gh")
    assert (code, label, needed) == (
        "wait_merge",
        "The publisher merges; nothing for you to do",
        False,
    )

    # 3. REWORK
    rec_rew = _make_issue("ISS-3", "P1", 3, IssueState.REWORK, contributor_id="C1")
    code, label, needed = _contributor_next_action(rec_rew, "C1", "c1_gh")
    assert (code, label, needed) == ("rework", "Answer the findings and push", True)

    # 4. IN_REVIEW -> wait verdict
    claim = Claim(contributor_id="C1", issued_at=now, expires_at=now + timedelta(days=7))
    sub = Submission(
        pr_number=10,
        head_sha="abc",
        checks_passed=True,
        files_changed=2,
        additions=20,
        deletions=5,
    )
    rec_in_rev = _make_issue(
        "ISS-4",
        "P1",
        4,
        IssueState.IN_REVIEW,
        contributor_id="C1",
        claim=claim,
        submission=sub,
    )
    code, label, needed = _contributor_next_action(rec_in_rev, "C1", "c1_gh")
    assert (code, label, needed) == ("wait_verdict", "Wait for the verdict", False)

    # 5. Claimed, no submission -> open_pr
    rec_claimed = _make_issue(
        "ISS-5", "P1", 5, IssueState.FUNDED, contributor_id="C1", claim=claim
    )
    code, label, needed = _contributor_next_action(rec_claimed, "C1", "c1_gh")
    assert (code, label, needed) == (
        "open_pr",
        "Open the pull request, or submit the one you opened",
        True,
    )


def test_publisher_and_contributor_dashboard_queries():
    store.ensure_ready()
    # Publisher test
    pub = Publisher(
        id="pub_test_dash_1",
        name="Test Publisher Org",
        kind="company",
        tier="team",
        wallet=Wallet(address="0x1111111111111111111111111111111111111111", chain="5042002"),
        budget_remaining_usdc="4500.00",
    )
    store.save_publisher(pub)

    # Contributor test
    con = Contributor(
        id="con_test_dash_1",
        handle="charlie",
        wallet=Wallet(address="0x2222222222222222222222222222222222222222", chain="5042002"),
        reputation=88,
        settled_issues=2,
        earned_usdc="500.00",
    )
    store.save_contributor(con)

    issue_pub1 = _make_issue(
        "ISS-DASH-P1",
        "pub_test_dash_1",
        101,
        IssueState.AWAITING_APPROVAL,
        title="Publisher 1 issue",
        price=Usdc(150_000_000),
    )
    issue_pub2 = _make_issue(
        "ISS-DASH-P2",
        "pub_other",
        202,
        IssueState.FUNDED,
        title="Publisher 2 issue",
        price=Usdc(300_000_000),
    )
    issue_con1 = _make_issue(
        "ISS-DASH-C1",
        "pub_test_dash_1",
        102,
        IssueState.ACCEPTED,
        title="Charlie work",
        price=Usdc(250_000_000),
        contributor_id="con_test_dash_1",
    )
    store.repo.save_issues(issue_pub1, issue_pub2, issue_con1)

    # 1. Publisher Dashboard for pub_test_dash_1
    dash_p = store.publisher_dashboard("pub_test_dash_1", "testorg")
    assert dash_p.party_id == "pub_test_dash_1"
    assert dash_p.name == "Test Publisher Org"
    assert dash_p.budget_remaining_usdc == 4500.0
    # Must NOT contain pub_other's issue
    pub_issue_ids = [i.id for i in dash_p.all_issues]
    assert "ISS-DASH-P1" in pub_issue_ids
    assert "ISS-DASH-C1" in pub_issue_ids
    assert "ISS-DASH-P2" not in pub_issue_ids

    # 2. Contributor Dashboard for con_test_dash_1
    dash_c = store.contributor_dashboard("con_test_dash_1", "charlie")
    assert dash_c.party_id == "con_test_dash_1"
    assert dash_c.handle == "charlie"
    assert dash_c.payout_wallet_set is True
    assert dash_c.payout_address == "0x2222222222222222222222222222222222222222"
    con_issue_ids = [w.id for w in dash_c.all_work]
    assert "ISS-DASH-C1" in con_issue_ids
    assert "ISS-DASH-P1" not in con_issue_ids  # Isolation: Charlie didn't work on P1
    assert "ISS-DASH-P2" not in con_issue_ids
    assert dash_c.about_to_release_usdc == 250.0

"""A GitHub issue is listed once while it is open, and a pull request counts once (#120).

Publishing a repo#number that was already open on Misthos made a second issue, and a
double click on "Publish and get a price" did the same. One pull request submitted to
both was then reviewed, accepted and paid on each. A second publish of an open issue
is refused now, naming the listing it has, under a lock two publishes at once cannot
both pass; and a pull request submitted for one issue is refused for any other.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from siwe_wallet import Wallet

from misthos.api.v1.webhooks import signature_for
from misthos.config import settings
from misthos.domain.issue import ESCROW_TERM, TERMINAL_STATES, IssueState
from misthos.main import app
from misthos.models.records import IssueRecord
from misthos.schemas import PublishRequest
from misthos.services.coordination import Busy
from misthos.services.github import PullRequest, SimulatedGitHub
from misthos.services.github.events import dispatch
from misthos.store import AlreadyListed, NotTheSubmission, Store, store
from misthos.workers.sweeper import sweep_once

API = "/api/v1"
REPO = "acme/widgets"


def request(number: int | None = 5, repo: str = REPO, publisher: str = "PUB-1") -> PublishRequest:
    return PublishRequest(repo=repo, number=number, title="Retry on 503", publisher_id=publisher)


def get(store: Store, issue_id: str) -> IssueRecord:
    rec = store.get(issue_id)
    assert rec is not None
    return rec


def open_listings(store: Store, repo: str, number: int) -> list[str]:
    return [
        r.id
        for r in store.list_issues()
        if r.repo.lower() == repo.lower()
        and r.number == number
        and r.state not in TERMINAL_STATES
    ]


def fund_and_claim(store: Store, issue_id: str, contributor_id: str = "CON-1") -> None:
    store.approve_criteria(issue_id, get(store, issue_id).acceptance_criteria, by="acme")
    store.approve_price(issue_id, by="acme")
    store.claim(issue_id, contributor_id)


def pull(number: int = 477, *, repo: str = REPO, author: str = "amara.dev") -> PullRequest:
    return PullRequest(
        repo=repo, number=number, author=author, head_sha="a" * 40,
        body="Fixes #5, fixes #6", merged=False, files_changed=3, additions=40, deletions=2,
    )  # fmt: skip


def slow_github(monkeypatch: pytest.MonkeyPatch, github: object) -> None:
    """Make reading the issue from GitHub take a moment, as it does for real, so every
    racer is past the first look for a listing before any of them has saved one."""
    read = github.read_issue  # type: ignore[attr-defined]

    def slowly(repo: str, number: int) -> object:
        time.sleep(0.2)
        return read(repo, number)

    monkeypatch.setattr(github, "read_issue", slowly)


def race(racers: int, run: Callable[[int], object]) -> list[object]:
    """Run `run` on `racers` threads released together, and return what each returned
    or raised."""
    start = threading.Barrier(racers)
    results: list[object] = [None] * racers

    def go(i: int) -> None:
        start.wait()
        try:
            results[i] = run(i)
        except Exception as exc:  # each outcome is the test's to judge
            results[i] = exc

    threads = [threading.Thread(target=go, args=(i,)) for i in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


class TestOneOpenListing:
    def test_a_second_publish_of_an_open_issue_is_refused_naming_the_first(
        self, every_store: Store
    ) -> None:
        first = every_store.publish(request())

        with pytest.raises(AlreadyListed, match=f"already on Misthos as {first.id}") as refused:
            every_store.publish(request())

        assert refused.value.issue_id == first.id
        assert open_listings(every_store, REPO, 5) == [first.id]

    def test_the_repository_is_the_same_whatever_its_case(self, every_store: Store) -> None:
        first = every_store.publish(request(repo="Acme/Widgets"))
        with pytest.raises(AlreadyListed) as refused:
            every_store.publish(request(repo="acme/WIDGETS"))
        assert refused.value.issue_id == first.id

    @pytest.mark.parametrize(
        ("issue_id", "repo", "number"),
        [
            ("ISS-1006", "acme/ledger-adapters", 178),  # AWAITING_APPROVAL
            ("ISS-1001", "acme/ledger-core", 412),  # FUNDED
            ("ISS-1003", "globex/parse-locale", 87),  # CLAIMED
            ("ISS-1002", "acme/ledger-core", 428),  # IN_REVIEW
            ("ISS-1007", "northwind/httpx-retry", 231),  # REWORK
        ],
    )
    def test_every_state_short_of_paid_or_refunded_holds_the_listing(
        self, every_store: Store, issue_id: str, repo: str, number: int
    ) -> None:
        with pytest.raises(AlreadyListed) as refused:
            every_store.publish(request(number, repo=repo))
        assert refused.value.issue_id == issue_id

    @pytest.mark.parametrize(
        ("issue_id", "repo", "number"),
        [
            ("ISS-1004", "northwind/httpx-retry", 219),  # PAID
            ("ISS-1008", "contoso/schema-cli", 61),  # REFUNDED, never re-listed
        ],
    )
    def test_a_paid_or_refunded_issue_can_be_listed_again(
        self, every_store: Store, issue_id: str, repo: str, number: int
    ) -> None:
        assert get(every_store, issue_id).state in TERMINAL_STATES
        again = every_store.publish(request(number, repo=repo))
        assert open_listings(every_store, repo, number) == [again.id]

    def test_a_refund_the_issue_was_re_listed_after_still_holds_it(
        self, every_store: Store
    ) -> None:
        """Nobody claimed it, so the refund re-listed it higher for the publisher to
        approve: that re-listing is the open one, and a publish is told so."""
        funded = get(every_store, "ISS-1001")
        assert funded.deadline is not None
        sweep_once(every_store, now=funded.deadline + ESCROW_TERM)
        [relisted] = open_listings(every_store, "acme/ledger-core", 412)
        assert get(every_store, relisted).relisted_from == "ISS-1001"

        with pytest.raises(AlreadyListed) as refused:
            every_store.publish(request(412, repo="acme/ledger-core"))
        assert refused.value.issue_id == relisted

    def test_a_refund_after_a_claim_lapsed_leaves_the_issue_free_to_list(
        self, every_store: Store
    ) -> None:
        claimed = get(every_store, "ISS-1003")
        assert claimed.deadline is not None
        sweep_once(every_store, now=claimed.deadline + ESCROW_TERM)
        assert get(every_store, "ISS-1003").state is IssueState.REFUNDED

        again = every_store.publish(request(87, repo="globex/parse-locale", publisher="PUB-4"))
        assert open_listings(every_store, "globex/parse-locale", 87) == [again.id]

    def test_a_form_with_no_number_is_given_one_the_repository_has_free(
        self, every_store: Store
    ) -> None:
        """The simulation makes the issue up, so its number must never collide with a
        listing the publisher did not mean."""
        numbers = [every_store.publish(request(None)).number for _ in range(12)]
        assert len(set(numbers)) == len(numbers)

    def test_two_publishes_at_once_list_the_issue_once(
        self, every_store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A double click, or GitHub's `opened` and `labeled` arriving together: each
        finds the issue unlisted, and only one may be saved."""
        slow_github(monkeypatch, every_store.github)
        racers = 6
        results = race(racers, lambda _: every_store.publish(request()))

        listed = [r.id for r in results if isinstance(r, IssueRecord)]
        refused = [r for r in results if not isinstance(r, IssueRecord)]
        assert len(listed) == 1, results
        assert all(isinstance(r, AlreadyListed) for r in refused), refused
        assert {r.issue_id for r in refused if isinstance(r, AlreadyListed)} == set(listed)
        assert open_listings(every_store, REPO, 5) == listed


class TestOnePullRequestCountsOnce:
    def test_a_pull_request_submitted_for_one_issue_is_refused_for_another(
        self, every_store: Store
    ) -> None:
        five, six = every_store.publish(request(5)), every_store.publish(request(6))
        for rec in (five, six):
            fund_and_claim(every_store, rec.id)
        every_store.submit_pull_request(five.id, pull())

        with pytest.raises(NotTheSubmission, match=f"already submitted for {five.id}"):
            every_store.submit_pull_request(six.id, pull())

        assert get(every_store, six.id).state is IssueState.CLAIMED
        assert get(every_store, six.id).submission is None

    def test_a_pull_request_already_paid_is_refused_for_a_new_listing(
        self, every_store: Store
    ) -> None:
        paid = get(every_store, "ISS-1004")  # PR #611, paid to priya.codes
        assert paid.state is IssueState.PAID and paid.submission is not None
        again = every_store.publish(request(219, repo=paid.repo, publisher=paid.publisher_id))
        fund_and_claim(every_store, again.id, "CON-3")

        with pytest.raises(NotTheSubmission, match="already submitted for ISS-1004"):
            every_store.submit_pull_request(
                again.id, pull(611, repo=paid.repo, author="priya.codes")
            )

    def test_one_pull_request_submitted_to_two_issues_at_once_counts_once(
        self, every_store: Store
    ) -> None:
        five, six = every_store.publish(request(5)), every_store.publish(request(6))
        for rec in (five, six):
            fund_and_claim(every_store, rec.id)
        targets = [five.id, six.id]

        results = race(2, lambda i: every_store.submit_pull_request(targets[i], pull()))

        submitted = [r for r in results if isinstance(r, IssueRecord)]
        refused = [r for r in results if not isinstance(r, IssueRecord)]
        assert len(submitted) == 1, results
        assert all(isinstance(r, NotTheSubmission | Busy) for r in refused), refused
        with_it = [
            i for i in targets if (s := get(every_store, i).submission) and s.pr_number == 477
        ]
        assert with_it == [submitted[0].id]


    def test_the_simulation_never_opens_a_pull_request_another_issue_counts(
        self, every_store: Store
    ) -> None:
        """It numbers a pull request after its issue, plus 400: acme/ledger-core#503's
        would be #903, the one ISS-1002 was submitted with, and #504's would be the one
        #503 took instead. The demo stepper opens its own the same way."""
        by_hand = every_store.publish(request(503, repo="acme/ledger-core"))
        stepped = every_store.publish(request(504, repo="acme/ledger-core"))
        for rec in (by_hand, stepped):
            fund_and_claim(every_store, rec.id)

        opened = every_store.demo_pull_request(by_hand.id)
        assert opened.number != 903
        assert every_store.submit_pull_request(by_hand.id, opened).state is IssueState.IN_REVIEW
        advanced = every_store.advance(stepped.id)
        assert advanced.state is IssueState.IN_REVIEW and advanced.submission is not None
        assert advanced.submission.pr_number not in {903, opened.number}


@pytest.fixture
def fresh_api_store() -> SimulatedGitHub:
    store.reset()
    assert isinstance(store.github, SimulatedGitHub)
    return store.github


class TestTheApi:
    def test_publishing_an_open_issue_again_is_a_409_naming_it(
        self, fresh_api_store: SimulatedGitHub
    ) -> None:
        client = TestClient(app)
        body = {"repo": REPO, "number": 5, "title": "Retry on 503", "publisher_id": "PUB-1"}
        first = client.post(f"{API}/issues", json=body)
        assert first.status_code == 201, first.text

        again = client.post(f"{API}/issues", json={**body, "repo": "ACME/widgets"})

        assert again.status_code == 409
        refused = again.json()
        assert refused["issue_id"] == first.json()["id"]
        assert first.json()["id"] in refused["detail"]
        listed = client.get(f"{API}/issues").json()
        assert [i["id"] for i in listed if i["repo"] == REPO and i["number"] == 5] == [
            first.json()["id"]
        ]


def _deliver(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    r = TestClient(app).post(
        f"{API}/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": signature_for(body, settings.github_webhook_secret),
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 202, r.text
    return r.json()


def _labelled(action: str) -> dict[str, Any]:
    return {
        "action": action, "repository": {"full_name": REPO}, "label": {"name": "misthos"},
        "issue": {"number": 12, "title": "Retry on 503",
                  "labels": [{"name": "bug"}, {"name": "misthos"}]},
    }  # fmt: skip


class TestTheLabelOnGitHub:
    @pytest.fixture
    def connected(self, fresh_api_store: SimulatedGitHub) -> SimulatedGitHub:
        client = TestClient(app)
        wallet = Wallet(91)
        nonce = client.post(f"{API}/auth/nonce").json()["nonce"]
        message = wallet.message(nonce)
        client.post(
            f"{API}/auth/verify", json={"message": message, "signature": wallet.sign(message)}
        )
        client.post(f"{API}/auth/role", json={"role": "publisher", "name": "Acme"})
        client.post(f"{API}/auth/github/simulate", json={"login": "acme-maint"})
        _deliver("installation", {
            "action": "created", "installation": {"id": 42}, "sender": {"login": "acme-maint"},
            "repositories": [{"full_name": REPO}],
        })  # fmt: skip
        return fresh_api_store

    def priced(self, github: SimulatedGitHub) -> list[str]:
        return [
            s.body for s in github.sent
            if s.kind == "comment" and s.target == "12" and "Priced on Misthos" in s.body
        ]  # fmt: skip

    def test_labelling_a_listed_issue_again_does_not_list_it_twice(
        self, connected: SimulatedGitHub
    ) -> None:
        assert _deliver("issues", _labelled("labeled"))["handled"] is True
        again = _deliver("issues", _labelled("labeled"))

        assert len(open_listings(store, REPO, 12)) == 1
        assert again["issue_ids"] == open_listings(store, REPO, 12)
        assert len(self.priced(connected)) == 1

    def test_opened_and_labeled_arriving_together_list_the_issue_once(
        self, connected: SimulatedGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GitHub sends both for an issue created with the label."""
        slow_github(monkeypatch, connected)
        events = [_labelled("opened"), _labelled("labeled")]
        results = race(2, lambda i: dispatch(store, "issues", events[i]))

        assert not [r for r in results if isinstance(r, Exception)], results
        assert len(open_listings(store, REPO, 12)) == 1
        assert len(self.priced(connected)) == 1

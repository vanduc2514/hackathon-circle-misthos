"""The repository protocol and the two ways a save can be refused."""

from __future__ import annotations

from collections.abc import Collection
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from misthos.domain.compliance import PartyKind, Screening
from misthos.domain.issue import IssueState
from misthos.domain.paid import PaidCall, PaidReview, PriceQuote
from misthos.models.records import IssueRecord
from misthos.schemas import (
    Account,
    Contributor,
    Decision,
    Publisher,
    RepoConnection,
)


class StaleIssue(Exception):
    """The issue was saved by someone else after this copy was loaded.

    Overwriting would lose their change, which in this system can be a refund or a
    release, so the save is refused and the caller reloads and decides again.
    """

    def __init__(self, issue_id: str) -> None:
        super().__init__(f"issue {issue_id} changed since it was loaded; reload and retry")
        self.issue_id = issue_id


class AppendOnlyViolation(Exception):
    """A save would remove or rewrite a decision that is already on record."""


class AccountConflict(Exception):
    """A GitHub login can belong to one wallet only."""


class Repository(Protocol):
    def next_value(self, counter: str, start: int) -> int:
        """The next value of a named sequence shared by every process."""

    def save_publisher(self, publisher: Publisher) -> None: ...

    def get_publisher(self, publisher_id: str) -> Publisher | None: ...

    def list_publishers(self) -> list[Publisher]: ...

    def save_contributor(self, contributor: Contributor) -> None: ...

    def get_contributor(self, contributor_id: str) -> Contributor | None: ...

    def list_contributors(self) -> list[Contributor]: ...

    def record_screening(self, screening: Screening) -> None:
        """Append one sanctions check to the audit trail. Never edited."""

    def latest_screening(self, party_kind: PartyKind, party_id: str) -> Screening | None: ...

    def list_screenings(self, party_kind: PartyKind, party_id: str) -> list[Screening]:
        """Every check of one party, oldest first."""

    def purge_screenings(self, before: datetime) -> int:
        """Delete checks older than the retention period. Returns how many went."""

    def get_account(self, address: str) -> Account | None: ...

    def save_account(self, account: Account) -> None:
        """Create or update. A GitHub login already linked to another wallet, or held
        for one (`hold_github_login`), is refused with AccountConflict."""

    def get_account_by_github_login(self, login: str) -> Account | None: ...

    def get_pending_github_login(self, address: str) -> str | None:
        """The login held for a wallet that has no account yet, as GitHub spells it."""

    def hold_github_login(self, address: str, login: str, at: datetime) -> None:
        """Hold a login for a wallet before its account exists (#181), replacing any
        login it held before. A login linked to another wallet, or held for one, is
        refused with AccountConflict: a pending link takes the one-login-one-wallet slot
        as much as a finished one, whatever the case it is written in."""

    def drop_pending_github_login(self, address: str) -> None:
        """Forget the wallet's held login, once its account has adopted it."""

    def get_connection(self, repo: str) -> RepoConnection | None: ...

    def save_connection(self, connection: RepoConnection) -> None: ...

    def delete_connections(self, repos: list[str]) -> None: ...

    def list_connections(self) -> list[RepoConnection]: ...

    def record_delivery(self, delivery_id: str, event: str, at: datetime) -> bool:
        """Note a webhook delivery. False if it was already noted: GitHub redelivers,
        and a redelivered merge must not be a second acceptance."""

    def forget_delivery(self, delivery_id: str) -> None:
        """Drop a delivery that failed, so its redelivery is handled."""

    def get_issue(self, issue_id: str) -> IssueRecord | None:
        """A private copy: changing it changes nothing until it is saved."""

    def list_issues(self, states: Collection[IssueState] | None = None) -> list[IssueRecord]: ...

    def save_issues(self, *records: IssueRecord) -> None:
        """Save every record or none of them, bumping each one's version.

        Raises StaleIssue when any record's version is not the stored one, and
        AppendOnlyViolation when a record has fewer decisions than are stored.
        """

    def list_decisions(self, limit: int) -> list[Decision]:
        """The decision log across every issue, newest first."""

    def page_decisions(
        self,
        *,
        after: tuple[datetime, str] | None = None,
        limit: int = 50,
        issue_id: str | None = None,
        actor: str | None = None,
        action: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Decision]:
        """One page of the decision log, oldest first, in (created_at, id) order and
        starting just after `after`. For the paid export (#202), which pages through the
        whole log where the public read shows only the newest."""

    def record_paid_call(self, call: PaidCall) -> PaidCall:
        """Append one settled x402 call (#201) and return the call on record. When the
        same route, request and settlement are already recorded, nothing is written and
        that call is returned: the edge may send a settlement twice."""

    def get_paid_call(self, call_id: str) -> PaidCall | None: ...

    def list_paid_calls(
        self, *, since: datetime | None = None, route: str | None = None, limit: int = 500
    ) -> list[PaidCall]:
        """Settled calls, newest first."""

    def record_price_quote(self, quote: PriceQuote) -> None:
        """Append one quote the paid API gave (#203). Never edited."""

    def list_price_quotes(self, limit: int = 500) -> list[PriceQuote]:
        """Quotes, newest first."""

    def save_paid_review(self, review: PaidReview) -> None:
        """Create or update a paid review's job (#204)."""

    def get_paid_review(self, review_id: str) -> PaidReview | None: ...

    def try_lock(self, name: str) -> AbstractContextManager[bool]:
        """Hold a named lock for the duration of the block if nobody else holds it.

        Yields whether it was acquired, without waiting, so a second sweeper skips
        its turn rather than queueing behind the first.
        """

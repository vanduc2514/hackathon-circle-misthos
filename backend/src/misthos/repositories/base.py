"""The repository protocol and the two ways a save can be refused."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from misthos.domain.compliance import PartyKind, Screening
from misthos.domain.issue import IssueState
from misthos.models.records import IssueRecord
from misthos.schemas import (
    Account,
    Contributor,
    Decision,
    Publisher,
    RepoConnection,
    SsoConnection,
    Subscription,
    SubscriptionPayment,
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
    """A wallet, a GitHub account and a GitHub login each belong to one account only."""


class SsoDomainTaken(Exception):
    """An e-mail domain routes sign-ins to one organisation's identity provider only."""


class PaymentAlreadyUsed(Exception):
    """A transaction pays for one subscription period, once."""


@dataclass(frozen=True)
class Seed:
    """What the simulation's reset starts the repository from.

    It is built in a repository of its own and handed over whole, so the one being
    reset goes from its old contents to these in one step. The issues' versions are
    not carried over: the repository being reset numbers them.
    """

    publishers: tuple[Publisher, ...]
    contributors: tuple[Contributor, ...]
    issues: tuple[IssueRecord, ...]
    counters: Mapping[str, int]


class Repository(Protocol):
    def reset(self, seed: Seed | None = None) -> None:
        """Remove everything, then hold `seed` if one is given, as one step.

        A reader sees the old contents or the new ones, never the empty repository in
        between. A copy of an issue loaded before the reset is stale after it, even
        though the seed brings back an issue with the same id: versions never repeat.
        The simulation's reset; never wired to production.
        """

    def is_empty(self) -> bool: ...

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

    def get_account(self, party_id: str) -> Account | None:
        """The account of a publisher or contributor, by the party's id."""

    def get_account_by_address(self, address: str) -> Account | None:
        """The account a wallet is connected to, whatever the case of the address."""

    def get_account_by_github_id(self, github_id: int) -> Account | None: ...

    def get_account_by_github_login(self, login: str) -> Account | None:
        """Whatever the case of the login, as GitHub matches it."""

    def save_account(self, account: Account) -> None:
        """Create or update, by party id. A wallet, GitHub id or GitHub login that
        already belongs to another account is refused with AccountConflict."""

    def get_connection(self, repo: str) -> RepoConnection | None: ...

    def save_connection(self, connection: RepoConnection) -> None: ...

    def delete_connections(self, repos: list[str]) -> None: ...

    def list_connections(self) -> list[RepoConnection]: ...

    def get_sso(self, publisher_id: str) -> SsoConnection | None: ...

    def get_sso_by_domain(self, domain: str) -> SsoConnection | None:
        """The connection that speaks for an e-mail domain, lowercase."""

    def save_sso(self, connection: SsoConnection) -> None:
        """Create or replace, by publisher. A domain another organisation's connection
        names is refused with SsoDomainTaken, and nothing is written."""

    def delete_sso(self, publisher_id: str) -> None: ...

    def get_subscription(self, publisher_id: str) -> Subscription | None: ...

    def save_subscription(self, subscription: Subscription) -> None: ...

    def list_subscriptions(self) -> list[Subscription]: ...

    def add_subscription_payment(self, payment: SubscriptionPayment) -> None:
        """Append one payment. A transaction already recorded is refused with
        PaymentAlreadyUsed."""

    def list_subscription_payments(self, publisher_id: str) -> list[SubscriptionPayment]:
        """Oldest first."""

    def record_delivery(self, delivery_id: str, event: str, at: datetime) -> bool:
        """Note a webhook delivery. False if it was already noted: GitHub redelivers,
        and a redelivered merge must not be a second acceptance."""

    def forget_delivery(self, delivery_id: str) -> None:
        """Drop a delivery that failed, so its redelivery is handled."""

    def get_issue(self, issue_id: str) -> IssueRecord | None:
        """A private copy: changing it changes nothing until it is saved."""

    def list_issues(self, states: Collection[IssueState] | None = None) -> list[IssueRecord]: ...

    def count_issues(self) -> int: ...

    def save_issues(self, *records: IssueRecord) -> None:
        """Save every record or none of them, bumping each one's version.

        Raises StaleIssue when any record's version is not the stored one, and
        AppendOnlyViolation when a record has fewer decisions than are stored.
        """

    def list_decisions(self, limit: int) -> list[Decision]:
        """The decision log across every issue, newest first."""

    def try_lock(self, name: str) -> AbstractContextManager[bool]:
        """Hold a named lock for the duration of the block if nobody else holds it.

        Yields whether it was acquired, without waiting, so a second sweeper skips
        its turn rather than queueing behind the first.
        """

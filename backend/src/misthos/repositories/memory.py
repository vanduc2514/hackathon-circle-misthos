"""In-memory repository.

The zero-config default and the test double. It hands out deep copies and stores
deep copies, so code that forgets to save loses its change here exactly as it would
against a database, instead of passing tests by mutating a shared object.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from datetime import datetime

from misthos.domain.compliance import PartyKind, Screening
from misthos.domain.issue import IssueState
from misthos.models.records import IssueRecord
from misthos.repositories.base import (
    AccountConflict,
    AppendOnlyViolation,
    PaymentAlreadyUsed,
    Seed,
    StaleIssue,
)
from misthos.schemas import (
    Account,
    Contributor,
    Decision,
    Publisher,
    RepoConnection,
    Subscription,
    SubscriptionPayment,
)


class MemoryRepository:
    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._named_locks: dict[str, threading.Lock] = {}
        # The highest version any issue reached before the last reset. A reset keeps
        # it, and an issue saved for the first time starts above it, so a copy loaded
        # before a reset never matches the issue the seed brings back under its id.
        self._version_floor = 0
        self._issues: dict[str, IssueRecord] = {}
        self.reset()

    def reset(self, seed: Seed | None = None) -> None:
        # Everything below happens under the guard every reader takes, so nobody can
        # look in between the old contents going and the seed arriving.
        with self._guard:
            self._version_floor = max(
                [self._version_floor, *(r.version for r in self._issues.values())]
            )
            self._publishers: dict[str, Publisher] = {}
            self._contributors: dict[str, Contributor] = {}
            self._issues: dict[str, IssueRecord] = {}
            self._counters: dict[str, int] = {}
            self._screenings: list[Screening] = []
            self._deliveries: dict[str, tuple[str, datetime]] = {}
            self._accounts: dict[str, Account] = {}
            self._subscriptions: dict[str, Subscription] = {}
            self._connections: dict[str, RepoConnection] = {}
            self._subscription_payments: list[SubscriptionPayment] = []
            if seed is None:
                return
            for publisher in seed.publishers:
                self._publishers[publisher.id] = publisher.model_copy(deep=True)
            for contributor in seed.contributors:
                self._contributors[contributor.id] = contributor.model_copy(deep=True)
            for rec in seed.issues:
                stored = copy.deepcopy(rec)
                stored.version = self._version_floor + 1
                self._issues[rec.id] = stored
            self._counters = dict(seed.counters)

    def as_seed(self) -> Seed:
        """What this repository holds, as a seed another repository can be reset to."""
        with self._guard:
            # A seed carries parties, issues and the id sequences, and nothing else,
            # so anything else written here would be lost on the way.
            assert not (
                self._screenings
                or self._deliveries
                or self._accounts
                or self._subscriptions
                or self._connections
                or self._subscription_payments
            ), "a seed holds publishers, contributors, issues and counters only"
            return Seed(
                publishers=tuple(p.model_copy(deep=True) for p in self._publishers.values()),
                contributors=tuple(c.model_copy(deep=True) for c in self._contributors.values()),
                issues=tuple(copy.deepcopy(r) for r in self._issues.values()),
                counters=dict(self._counters),
            )

    def is_empty(self) -> bool:
        with self._guard:
            return not self._publishers and not self._issues

    def next_value(self, counter: str, start: int) -> int:
        with self._guard:
            value = self._counters.get(counter, start - 1) + 1
            self._counters[counter] = value
            return value

    # ------------------------------------------------------------ parties

    def save_publisher(self, publisher: Publisher) -> None:
        with self._guard:
            self._publishers[publisher.id] = publisher.model_copy(deep=True)

    def get_publisher(self, publisher_id: str) -> Publisher | None:
        with self._guard:
            found = self._publishers.get(publisher_id)
            return found.model_copy(deep=True) if found else None

    def list_publishers(self) -> list[Publisher]:
        with self._guard:
            return [p.model_copy(deep=True) for p in self._publishers.values()]

    def save_contributor(self, contributor: Contributor) -> None:
        with self._guard:
            self._contributors[contributor.id] = contributor.model_copy(deep=True)

    def get_contributor(self, contributor_id: str) -> Contributor | None:
        with self._guard:
            found = self._contributors.get(contributor_id)
            return found.model_copy(deep=True) if found else None

    def list_contributors(self) -> list[Contributor]:
        with self._guard:
            return [c.model_copy(deep=True) for c in self._contributors.values()]

    # --------------------------------------------------------- screenings

    def record_screening(self, screening: Screening) -> None:
        with self._guard:
            self._screenings.append(screening)

    def latest_screening(self, party_kind: PartyKind, party_id: str) -> Screening | None:
        found = self.list_screenings(party_kind, party_id)
        return found[-1] if found else None

    def list_screenings(self, party_kind: PartyKind, party_id: str) -> list[Screening]:
        with self._guard:
            return [
                s
                for s in self._screenings
                if s.party_kind is party_kind and s.party_id == party_id
            ]

    def purge_screenings(self, before: datetime) -> int:
        with self._guard:
            kept = [s for s in self._screenings if s.checked_at >= before]
            purged = len(self._screenings) - len(kept)
            self._screenings = kept
            return purged

    # ------------------------------------------------------------ accounts

    def get_account(self, party_id: str) -> Account | None:
        with self._guard:
            found = self._accounts.get(party_id)
            return found.model_copy(deep=True) if found else None

    def _find_account(self, matches: Callable[[Account], bool]) -> Account | None:
        with self._guard:
            found = next((a for a in self._accounts.values() if matches(a)), None)
            return found.model_copy(deep=True) if found else None

    def get_account_by_address(self, address: str) -> Account | None:
        return self._find_account(
            lambda a: a.address is not None and a.address.lower() == address.lower()
        )

    def get_account_by_github_id(self, github_id: int) -> Account | None:
        return self._find_account(lambda a: a.github_id == github_id)

    def get_account_by_github_login(self, login: str) -> Account | None:
        return self._find_account(
            lambda a: a.github_login is not None and a.github_login.lower() == login.lower()
        )

    def save_account(self, account: Account) -> None:
        with self._guard:
            others = [a for a in self._accounts.values() if a.party_id != account.party_id]
            # The same three the database holds unique, so the test double refuses what
            # Postgres would; the login case-insensitively, as GitHub treats it.
            if account.address and any(
                (o.address or "").lower() == account.address.lower() for o in others
            ):
                raise AccountConflict("that wallet belongs to another account")
            if account.github_id is not None and any(
                o.github_id == account.github_id for o in others
            ):
                raise AccountConflict("that GitHub account is linked to another account")
            if account.github_login and any(
                (o.github_login or "").lower() == account.github_login.lower() for o in others
            ):
                raise AccountConflict(f"{account.github_login} is linked to another account")
            # Lowercase, as the database stores it, so both hand back the same address.
            address = account.address.lower() if account.address else None
            self._accounts[account.party_id] = account.model_copy(
                update={"address": address}, deep=True
            )

    # ------------------------------------------------------- GitHub connections

    def get_connection(self, repo: str) -> RepoConnection | None:
        with self._guard:
            found = self._connections.get(repo.lower())
            return found.model_copy(deep=True) if found else None

    def save_connection(self, connection: RepoConnection) -> None:
        with self._guard:
            # Stored lowercase, as the database stores it, so both hand back the same name.
            repo = connection.repo.lower()
            self._connections[repo] = connection.model_copy(update={"repo": repo}, deep=True)

    def delete_connections(self, repos: list[str]) -> None:
        with self._guard:
            for repo in repos:
                self._connections.pop(repo.lower(), None)

    def list_connections(self) -> list[RepoConnection]:
        with self._guard:
            return [c.model_copy(deep=True) for c in self._connections.values()]

    # ------------------------------------------------------------ plans

    def get_subscription(self, publisher_id: str) -> Subscription | None:
        with self._guard:
            found = self._subscriptions.get(publisher_id)
            return found.model_copy(deep=True) if found else None

    def save_subscription(self, subscription: Subscription) -> None:
        with self._guard:
            self._subscriptions[subscription.publisher_id] = subscription.model_copy(deep=True)

    def list_subscriptions(self) -> list[Subscription]:
        with self._guard:
            return [s.model_copy(deep=True) for s in self._subscriptions.values()]

    def add_subscription_payment(self, payment: SubscriptionPayment) -> None:
        with self._guard:
            if any(
                p.tx_hash.lower() == payment.tx_hash.lower() for p in self._subscription_payments
            ):
                raise PaymentAlreadyUsed(payment.tx_hash)
            self._subscription_payments.append(payment.model_copy(deep=True))

    def list_subscription_payments(self, publisher_id: str) -> list[SubscriptionPayment]:
        with self._guard:
            return [
                p.model_copy(deep=True)
                for p in self._subscription_payments
                if p.publisher_id == publisher_id
            ]

    # ---------------------------------------------------------- deliveries

    def record_delivery(self, delivery_id: str, event: str, at: datetime) -> bool:
        with self._guard:
            if delivery_id in self._deliveries:
                return False
            self._deliveries[delivery_id] = (event, at)
            return True

    def forget_delivery(self, delivery_id: str) -> None:
        with self._guard:
            self._deliveries.pop(delivery_id, None)

    # ------------------------------------------------------------- issues

    def get_issue(self, issue_id: str) -> IssueRecord | None:
        with self._guard:
            found = self._issues.get(issue_id)
            return copy.deepcopy(found) if found else None

    def list_issues(self, states: Collection[IssueState] | None = None) -> list[IssueRecord]:
        with self._guard:
            return [
                copy.deepcopy(r)
                for r in self._issues.values()
                if states is None or r.state in states
            ]

    def count_issues(self) -> int:
        with self._guard:
            return len(self._issues)

    def save_issues(self, *records: IssueRecord) -> None:
        with self._guard:
            # Check every record before writing any, so the save is all or nothing.
            for rec in records:
                stored = self._issues.get(rec.id)
                if (stored.version if stored else 0) != rec.version:
                    raise StaleIssue(rec.id)
                if stored and len(rec.decisions) < len(stored.decisions):
                    raise AppendOnlyViolation(f"issue {rec.id} would lose decisions")
                if stored and len(rec.money_events) < len(stored.money_events):
                    raise AppendOnlyViolation(f"issue {rec.id} would lose money events")
            for rec in records:
                stored = self._issues.get(rec.id)
                rec.version = stored.version + 1 if stored else self._version_floor + 1
                self._issues[rec.id] = copy.deepcopy(rec)

    def list_decisions(self, limit: int) -> list[Decision]:
        with self._guard:
            rows = [d for r in self._issues.values() for d in r.decisions]
        rows.sort(key=lambda d: d.created_at, reverse=True)
        return [d.model_copy() for d in rows[:limit]]

    @contextmanager
    def try_lock(self, name: str) -> Iterator[bool]:
        with self._guard:
            lock = self._named_locks.setdefault(name, threading.Lock())
        acquired = lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()

"""In-memory repository.

The zero-config default and the test double. It hands out deep copies and stores
deep copies, so code that forgets to save loses its change here exactly as it would
against a database, instead of passing tests by mutating a shared object.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import datetime

from misthos.domain.compliance import PartyKind, Screening
from misthos.domain.issue import IssueState
from misthos.models.records import IssueRecord
from misthos.repositories.base import AccountConflict, AppendOnlyViolation, StaleIssue
from misthos.schemas import Account, Contributor, Decision, Publisher


class MemoryRepository:
    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._named_locks: dict[str, threading.Lock] = {}
        self.reset()

    def reset(self) -> None:
        with self._guard:
            self._publishers: dict[str, Publisher] = {}
            self._contributors: dict[str, Contributor] = {}
            self._issues: dict[str, IssueRecord] = {}
            self._counters: dict[str, int] = {}
            self._screenings: list[Screening] = []
            self._deliveries: dict[str, tuple[str, datetime]] = {}
            self._accounts: dict[str, Account] = {}

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

    def get_account(self, address: str) -> Account | None:
        with self._guard:
            found = self._accounts.get(address.lower())
            return found.model_copy(deep=True) if found else None

    def save_account(self, account: Account) -> None:
        with self._guard:
            if account.github_login and any(
                other.github_login
                and other.github_login.lower() == account.github_login.lower()
                and other.address != account.address
                for other in self._accounts.values()
            ):
                raise AccountConflict(f"{account.github_login} is linked to another wallet")
            self._accounts[account.address.lower()] = account.model_copy(deep=True)

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
                rec.version += 1
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

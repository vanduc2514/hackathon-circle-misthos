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
from misthos.domain.paid import PaidCall, PaidReview, PriceQuote
from misthos.models.records import IssueRecord
from misthos.repositories.base import (
    AccountConflict,
    AppendOnlyViolation,
    StaleIssue,
)
from misthos.schemas import (
    Account,
    Contributor,
    Decision,
    Publisher,
    RepoConnection,
)


class MemoryRepository:
    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._named_locks: dict[str, threading.Lock] = {}
        self._version_floor = 0
        self._issues: dict[str, IssueRecord] = {}
        self._publishers: dict[str, Publisher] = {}
        self._contributors: dict[str, Contributor] = {}
        self._counters: dict[str, int] = {}
        self._screenings: list[Screening] = []
        self._deliveries: dict[str, tuple[str, datetime]] = {}
        self._accounts: dict[str, Account] = {}
        self._pending_logins: dict[str, tuple[str, datetime]] = {}
        self._connections: dict[str, RepoConnection] = {}
        self._paid_calls: list[PaidCall] = []
        self._price_quotes: list[PriceQuote] = []
        self._paid_reviews: dict[str, PaidReview] = {}

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

    # ------------------------------------------------------- GitHub connections

    def get_account_by_github_login(self, login: str) -> Account | None:
        with self._guard:
            for account in self._accounts.values():
                if (account.github_login or "").lower() == login.lower():
                    return account.model_copy(deep=True)
            return None

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

    def save_account(self, account: Account) -> None:
        with self._guard:
            if account.github_login:
                self._refuse_taken(account.address, account.github_login)
            self._accounts[account.address.lower()] = account.model_copy(deep=True)

    def get_pending_github_login(self, address: str) -> str | None:
        with self._guard:
            held = self._pending_logins.get(address.lower())
            return held[0] if held else None

    def hold_github_login(self, address: str, login: str, at: datetime) -> None:
        with self._guard:
            self._refuse_taken(address, login)
            self._pending_logins[address.lower()] = (login, at)

    def drop_pending_github_login(self, address: str) -> None:
        with self._guard:
            self._pending_logins.pop(address.lower(), None)

    def _refuse_taken(self, address: str, login: str) -> None:
        """One login, one wallet: linked or held, and whatever its case. Under the guard."""
        address, wanted = address.lower(), login.lower()
        if any(
            other.github_login
            and other.github_login.lower() == wanted
            and other.address.lower() != address
            for other in self._accounts.values()
        ):
            raise AccountConflict(f"{login} is linked to another wallet")
        if any(
            held.lower() == wanted and holder != address
            for holder, (held, _) in self._pending_logins.items()
        ):
            raise AccountConflict(f"{login} is being linked by another wallet")

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
        with self._guard:
            rows = [d for r in self._issues.values() for d in r.decisions]
        kept = [
            d
            for d in rows
            if (after is None or (d.created_at, d.id) > after)
            and (issue_id is None or d.issue_id == issue_id)
            and (actor is None or d.actor == actor)
            and (action is None or d.action == action)
            and (since is None or d.created_at >= since)
            and (until is None or d.created_at < until)
        ]
        kept.sort(key=lambda d: (d.created_at, d.id))
        return [d.model_copy() for d in kept[:limit]]

    # ---------------------------------------------------------- paid calls

    def record_paid_call(self, call: PaidCall) -> PaidCall:
        with self._guard:
            key = (call.route, call.request_digest, call.transaction)
            for kept in self._paid_calls:
                if (kept.route, kept.request_digest, kept.transaction) == key:
                    return kept
            self._paid_calls.append(call)
            return call

    def get_paid_call(self, call_id: str) -> PaidCall | None:
        with self._guard:
            return next((c for c in self._paid_calls if c.id == call_id), None)

    def list_paid_calls(
        self, *, since: datetime | None = None, route: str | None = None, limit: int = 500
    ) -> list[PaidCall]:
        with self._guard:
            rows = [
                c
                for c in self._paid_calls
                if (since is None or c.settled_at >= since) and (route is None or c.route == route)
            ]
        rows.sort(key=lambda c: (c.settled_at, c.id), reverse=True)
        return rows[:limit]

    def record_price_quote(self, quote: PriceQuote) -> None:
        with self._guard:
            self._price_quotes.append(copy.deepcopy(quote))

    def list_price_quotes(self, limit: int = 500) -> list[PriceQuote]:
        with self._guard:
            rows = [copy.deepcopy(q) for q in self._price_quotes]
        rows.sort(key=lambda q: (q.quoted_at, q.id), reverse=True)
        return rows[:limit]

    def save_paid_review(self, review: PaidReview) -> None:
        with self._guard:
            self._paid_reviews[review.id] = copy.deepcopy(review)

    def get_paid_review(self, review_id: str) -> PaidReview | None:
        with self._guard:
            found = self._paid_reviews.get(review_id)
            return copy.deepcopy(found) if found else None

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

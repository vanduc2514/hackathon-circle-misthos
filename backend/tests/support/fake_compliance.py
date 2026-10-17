"""In-memory compliance providers for tests. Nothing here ships."""

from __future__ import annotations

import itertools
import threading
from collections.abc import Iterable

from misthos.domain.compliance import IdentityStatus, ScreeningOutcome


class FakeScreening:
    name = "fake"

    def __init__(self, listed: Iterable[str] = ()) -> None:
        self._guard = threading.Lock()
        self._listed = {a.strip().lower() for a in listed if a.strip()}
        self.outcome = ScreeningOutcome.CLEAR

    def list_wallet(self, wallet_address: str) -> None:
        with self._guard:
            self._listed.add(wallet_address.lower())

    def delist_wallet(self, wallet_address: str) -> None:
        with self._guard:
            self._listed.discard(wallet_address.lower())

    def screen(self, wallet_address: str) -> tuple[ScreeningOutcome, str | None]:
        with self._guard:
            if wallet_address.lower() in self._listed:
                return ScreeningOutcome.HIT, "FAKE-SDN"
        if self.outcome is not ScreeningOutcome.CLEAR:
            return self.outcome, "FAKE-SDN"
        return ScreeningOutcome.CLEAR, None


class FakeIdentity:
    name = "fake"

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._seq = itertools.count(1)
        self._sessions: dict[str, str] = {}
        self._pending: set[str] = set()
        self._failing: set[str] = set()

    def hold(self, contributor_id: str) -> None:
        self._pending.add(contributor_id)

    def release(self, contributor_id: str) -> None:
        self._pending.discard(contributor_id)

    def fail(self, contributor_id: str) -> None:
        self._failing.add(contributor_id)

    def start(self, contributor_id: str) -> str:
        with self._guard:
            reference = f"fake-idv-{next(self._seq):04d}"
            self._sessions[reference] = contributor_id
            return reference

    def status(self, reference: str) -> IdentityStatus:
        with self._guard:
            contributor_id = self._sessions.get(reference)
            if contributor_id is None:
                return IdentityStatus.UNVERIFIED
            if contributor_id in self._failing:
                return IdentityStatus.FAILED
            if contributor_id in self._pending:
                return IdentityStatus.PENDING
            return IdentityStatus.VERIFIED

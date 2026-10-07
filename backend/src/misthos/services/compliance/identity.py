"""Identity verification at first payout.

The provider holds the documents; the platform keeps a reference and an outcome. The
simulated provider approves a session the moment it is opened, so the demo shows a
first payout verifying its contributor in passing, and it can be told to hold or
fail one contributor so the tests can see both of those paths too.
"""

from __future__ import annotations

import itertools
import threading
from typing import Protocol

from misthos.domain.compliance import IdentityStatus


class IdentityProvider(Protocol):
    name: str

    def start(self, contributor_id: str) -> str:
        """Open a verification for this contributor and return the provider's reference.

        The contributor hands their documents to the provider, never to us.
        """

    def status(self, reference: str) -> IdentityStatus: ...


class SimulatedIdentity:
    name = "simulated"

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._seq = itertools.count(1)
        self._sessions: dict[str, str] = {}
        self._pending: set[str] = set()
        self._failing: set[str] = set()

    def hold(self, contributor_id: str) -> None:
        """Leave this contributor's verification pending until `release` is called."""
        with self._guard:
            self._pending.add(contributor_id)

    def release(self, contributor_id: str) -> None:
        with self._guard:
            self._pending.discard(contributor_id)

    def fail(self, contributor_id: str) -> None:
        with self._guard:
            self._failing.add(contributor_id)

    def start(self, contributor_id: str) -> str:
        with self._guard:
            reference = f"sim-idv-{next(self._seq):04d}"
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

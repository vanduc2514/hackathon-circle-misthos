"""The simulation's reset and the first seed, with other work going on at the same time.

A reset replaces every row, and for the moment it takes the API is still serving: four
people can be stepping the demo while a fifth presses reset. None of them may see the
store half reset, and a copy one of them loaded before the reset must not be saved over
the fresh seed after it. The first seed has its own race: an API process and a worker
starting together against an empty database both try to seed it, and the one that
loses must not decide the database is ready while it is still empty.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from misthos import store as store_module
from misthos.domain.compliance import ComplianceRefusal
from misthos.domain.issue import IllegalTransition
from misthos.domain.ledger import EscrowStatus
from misthos.domain.money import Usdc
from misthos.domain.policy import PolicyRefusal
from misthos.repositories import MemoryRepository, StaleIssue
from misthos.repositories.sql import SqlRepository
from misthos.services.chain import ChainRevert, SimulatedChain
from misthos.services.coordination import Busy
from misthos.services.github import GitHubError
from misthos.services.review import ReviewFailed
from misthos.store import (
    CriteriaNotApproved,
    DeclineRefused,
    DisputeRefused,
    NotSimulated,
    NotTheSubmission,
    Store,
    UntestableCriteria,
)

# What the API answers with a 409, a 403, a 422 or a 502. Anything else raised by an
# action is a 500.
REFUSALS = (
    IllegalTransition,
    StaleIssue,
    Busy,
    ChainRevert,
    DisputeRefused,
    DeclineRefused,
    CriteriaNotApproved,
    NotTheSubmission,
    ComplianceRefusal,
    PolicyRefusal,
    NotSimulated,
    UntestableCriteria,
    ReviewFailed,
    GitHubError,
)


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def resettable(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    if request.param == "memory":
        repo = MemoryRepository()
    elif request.param == "sqlite":
        repo = SqlRepository(f"sqlite:///{tmp_path / 'misthos.db'}")
    else:
        url = os.environ.get("MISTHOS_DATABASE_URL", "")
        if not url.startswith(("postgres://", "postgresql")):
            pytest.skip("set MISTHOS_DATABASE_URL to a Postgres database to run against it")
        repo = SqlRepository(url)
    store = Store(repo)
    store.reset()
    return store


class TestAResetNobodySeesHalfDone:
    def test_stepping_the_demo_while_it_resets_succeeds_or_is_refused_cleanly(
        self, resettable: Store
    ) -> None:
        """Four threads step every issue while a fifth resets the store again and again.

        Reset used to empty every table and then write the seed back one row at a time,
        so a step could find the issue gone (KeyError) or the contributors not written
        yet, and `contributors[0]` raised IndexError: both a 500. A step may still lose
        to the reset, but only with the refusal it would get from any other conflict.
        """
        store = resettable
        issue_ids = [r.id for r in store.list_issues()]
        stop = threading.Event()
        unexpected: list[BaseException] = []

        def step_the_demo() -> None:
            while not stop.is_set():
                for issue_id in issue_ids:
                    try:
                        store.advance(issue_id)
                    except REFUSALS:
                        pass
                    except Exception as exc:  # every other one is a 500
                        unexpected.append(exc)

        people = [threading.Thread(target=step_the_demo) for _ in range(4)]
        for person in people:
            person.start()
        try:
            for _ in range(12):
                store.reset()
        finally:
            stop.set()
            for person in people:
                person.join()

        assert [repr(e) for e in unexpected] == []
        assert store.count_issues() == len(issue_ids)


class TestACopyFromBeforeAReset:
    def test_is_refused_after_it(self, resettable: Store) -> None:
        """Versions used to start again at 1 with every seed, so a copy of ISS-1001
        loaded at version 1 before a reset matched the fresh row and overwrote it."""
        store = resettable
        stale = store.get("ISS-1001")
        assert stale is not None
        store.reset()

        stale.title = "a title from before the reset"
        with pytest.raises(StaleIssue):
            store.save(stale)
        again = store.get("ISS-1001")
        assert again is not None and again.title != "a title from before the reset"

    def test_is_refused_after_the_fresh_row_has_been_saved_as_often(
        self, resettable: Store
    ) -> None:
        """The fresh row's versions climb from above every version handed out before the
        reset, so they never come round to the one an old copy holds."""
        store = resettable
        for _ in range(3):
            store.save(store.get("ISS-1006"))  # type: ignore[arg-type]
        stale = store.get("ISS-1006")
        assert stale is not None
        store.reset()
        for _ in range(3):
            store.save(store.get("ISS-1006"))  # type: ignore[arg-type]

        with pytest.raises(StaleIssue):
            store.save(stale)

    def test_a_copy_loaded_after_the_reset_saves_as_usual(self, resettable: Store) -> None:
        store = resettable
        store.reset()
        mine = store.get("ISS-1001")
        assert mine is not None
        mine.title = "renamed after the reset"
        store.save(mine)
        assert store.get("ISS-1001").title == "renamed after the reset"  # type: ignore[union-attr]


class TestTheSimulatedChainAcrossAReset:
    def test_a_release_cut_in_half_by_a_reset_does_not_bring_the_commitment_back(
        self,
    ) -> None:
        """A release read the commitment, the reset forgot every commitment, and the
        release then wrote this one back, so the seed's own commitment to that issue
        was refused as AlreadyExists and the reset failed."""
        now = datetime.now(UTC)
        chain = SimulatedChain()
        chain.commit("ISS-1007", "0xpublisher", Usdc(5_000_000), now, now)
        resetting = threading.Thread(target=chain.reset)
        read = chain._get

        def read_then_let_the_reset_in(issue_id: str) -> tuple[str, Usdc, EscrowStatus] | None:
            found = read(issue_id)
            resetting.start()
            resetting.join(timeout=0.2)  # it runs here unless the release holds the books
            return found

        chain._get = read_then_let_the_reset_in  # type: ignore[method-assign]
        chain.release("ISS-1007", "0xcontributor", Usdc(5_000_000), now)
        chain._get = read  # type: ignore[method-assign]
        resetting.join()

        assert chain.commitments() == {}
        chain.commit("ISS-1007", "0xpublisher", Usdc(5_000_000), now, now)


class AnotherProcessHoldsTheSeedLock(MemoryRepository):
    """A database another process took the seed lock on as this one starts.

    It holds the lock until it has been asked for it `lets_go_after` times (or until
    the test calls `let_go`), and lets go with its seed written, or, when it died
    first, with nothing written at all.
    """

    def __init__(self, lets_go_after: int | None, *, seeds: bool) -> None:
        super().__init__()
        self.lets_go_after = lets_go_after
        self.seeds = seeds
        self.asked = 0
        self.holding = True

    def let_go(self) -> None:
        if self.seeds:
            Store(self)._seed()
        self.holding = False

    @contextmanager
    def try_lock(self, name: str) -> Iterator[bool]:
        if name == "seed" and self.holding:
            self.asked += 1
            if self.asked != self.lets_go_after:
                yield False
                return
            self.let_go()
        with super().try_lock(name) as held:
            yield held


class TestTheProcessThatLosesTheSeedRace:
    def test_waits_for_the_winner_and_serves_its_seed(self) -> None:
        """It used to mark itself ready on losing, while the database was still empty."""
        repo = AnotherProcessHoldsTheSeedLock(lets_go_after=3, seeds=True)
        assert Store(repo).count_issues() == 8
        assert repo.asked == 3

    def test_seeds_the_database_itself_when_the_winner_died_first(self) -> None:
        """Marked ready on losing, it never looked again, and served an empty database
        for the rest of its life."""
        repo = AnotherProcessHoldsTheSeedLock(lets_go_after=3, seeds=False)
        store = Store(repo)
        assert store.count_issues() == 8
        assert len(store.list_publishers()) == 5

    def test_that_gives_up_waiting_looks_again_on_its_next_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A winner slower than the wait leaves this process not ready, rather than
        ready and empty, so its next call takes the free lock and seeds."""
        monkeypatch.setattr(store_module, "SEED_WAIT", timedelta(milliseconds=200))
        repo = AnotherProcessHoldsTheSeedLock(lets_go_after=None, seeds=False)
        store = Store(repo)
        assert store.count_issues() == 0

        repo.let_go()
        assert store.count_issues() == 8

"""The coordinator: the per-issue lock, idempotency slots and rate-limit counters.

The same behaviour is owed by both implementations, so every test runs against the
in-process one and against Redis (fakeredis here; the backend-postgres CI job also
runs the whole suite against a real Redis).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import timedelta

import fakeredis
import pytest

from misthos.repositories import MemoryRepository
from misthos.services.coordination import (
    PREFIX,
    Coordinator,
    LocalCoordinator,
    RedisCoordinator,
    build_coordinator,
)

LONG = timedelta(minutes=5)
BRIEF = timedelta(milliseconds=50)


@pytest.fixture(params=["local", "redis"])
def coordinator(request: pytest.FixtureRequest) -> Iterator[Coordinator]:
    if request.param == "local":
        yield LocalCoordinator(MemoryRepository())
    else:
        yield RedisCoordinator(fakeredis.FakeRedis(decode_responses=True))


class TestLock:
    def test_one_holder_at_a_time(self, coordinator: Coordinator) -> None:
        with coordinator.lock("issue:ISS-1", LONG) as first:
            with coordinator.lock("issue:ISS-1", LONG) as second:
                assert first is True
                assert second is False

    def test_other_issues_are_not_held_up(self, coordinator: Coordinator) -> None:
        with coordinator.lock("issue:ISS-1", LONG) as first:
            with coordinator.lock("issue:ISS-2", LONG) as other:
                assert first and other

    def test_the_lock_is_free_again_after_the_block(self, coordinator: Coordinator) -> None:
        with coordinator.lock("issue:ISS-1", LONG):
            pass
        with coordinator.lock("issue:ISS-1", LONG) as again:
            assert again

    def test_the_lock_is_free_again_after_an_exception(self, coordinator: Coordinator) -> None:
        with pytest.raises(RuntimeError), coordinator.lock("issue:ISS-1", LONG):
            raise RuntimeError("the action failed")
        with coordinator.lock("issue:ISS-1", LONG) as again:
            assert again


class TestSlots:
    def test_reserve_only_sets_an_absent_key(self, coordinator: Coordinator) -> None:
        assert coordinator.reserve("idem:k", "first", LONG)
        assert not coordinator.reserve("idem:k", "second", LONG)
        assert coordinator.get("idem:k") == "first"

    def test_put_get_delete(self, coordinator: Coordinator) -> None:
        coordinator.put("idem:k", "v", LONG)
        assert coordinator.get("idem:k") == "v"
        coordinator.delete("idem:k")
        assert coordinator.get("idem:k") is None
        assert coordinator.reserve("idem:k", "again", LONG)

    def test_a_slot_lapses_after_its_ttl(self, coordinator: Coordinator) -> None:
        coordinator.put("idem:k", "v", BRIEF)
        time.sleep(0.12)
        assert coordinator.get("idem:k") is None


class TestCounters:
    def test_hits_count_up_within_a_window(self, coordinator: Coordinator) -> None:
        assert [coordinator.hit("rl:a:publish:1", LONG) for _ in range(3)] == [1, 2, 3]
        assert coordinator.hit("rl:b:publish:1", LONG) == 1

    def test_a_count_lapses_with_its_window(self, coordinator: Coordinator) -> None:
        coordinator.hit("rl:a:publish:1", BRIEF)
        time.sleep(0.12)
        assert coordinator.hit("rl:a:publish:1", BRIEF) == 1


def test_reset_forgets_everything(coordinator: Coordinator) -> None:
    coordinator.put("idem:k", "v", LONG)
    coordinator.hit("rl:a:publish:1", LONG)
    coordinator.reset()
    assert coordinator.get("idem:k") is None
    assert coordinator.hit("rl:a:publish:1", LONG) == 1


class TestRedis:
    def test_keys_live_under_the_service_prefix(self) -> None:
        client = fakeredis.FakeRedis(decode_responses=True)
        RedisCoordinator(client).put("idem:k", "v", LONG)
        assert client.get(f"{PREFIX}idem:k") == "v"

    def test_reset_leaves_other_services_keys_alone(self) -> None:
        client = fakeredis.FakeRedis(decode_responses=True)
        client.set("someone-else", "x")
        RedisCoordinator(client).reset()
        assert client.get("someone-else") == "x"

    def test_a_lapsed_lock_taken_by_someone_else_is_not_released_by_its_old_owner(
        self,
    ) -> None:
        client = fakeredis.FakeRedis(decode_responses=True)
        coordinator = RedisCoordinator(client)
        with coordinator.lock("issue:ISS-1", LONG) as held:
            assert held
            # The lock lapsed (a slow holder) and another process took it.
            client.set(f"{PREFIX}issue:ISS-1", "someone-else")
        assert client.get(f"{PREFIX}issue:ISS-1") == "someone-else"


def test_redis_is_used_only_when_configured() -> None:
    repo = MemoryRepository()
    assert isinstance(build_coordinator("", repo), LocalCoordinator)
    assert isinstance(build_coordinator("redis://localhost:6379/0", repo), RedisCoordinator)

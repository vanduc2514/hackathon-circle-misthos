"""Coordination between processes: one action per issue, idempotent money requests,
and rate limits.

The moment two API processes run, three things need an atomic place to live: the
lock that stops two people (or a person and the sweeper) acting on one issue at once,
the record of money requests already served so a retry after a timeout cannot pay
twice, and the counters that stop one client flooding publish and claim.

With `MISTHOS_REDIS_URL` set they live in Redis under the keys the architecture
names (`idem:{request_id}`, `rl:{actor}:{window}`) plus `issue:{issue_id}`, which
covers the claim and every other action on the issue. Without Redis the lock comes
from the repository, an advisory lock under Postgres and so still shared by every
process, while idempotency keys and counters stay in process memory, which is only
correct for a single process.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import timedelta
from typing import Any, Protocol

from misthos.repositories import Repository

# One action on an issue is a few database round trips and, later, one chain call
# with sub-second finality. A lock still held after this long belongs to a process
# that died, so Redis lets it lapse.
ISSUE_LOCK_TTL = timedelta(seconds=30)

PREFIX = "misthos:"


class Busy(Exception):
    """Someone else is acting on this issue right now."""

    def __init__(self, issue_id: str) -> None:
        super().__init__(f"another action on {issue_id} is in progress; retry in a moment")
        self.issue_id = issue_id


class Coordinator(Protocol):
    def lock(self, name: str, ttl: timedelta) -> AbstractContextManager[bool]:
        """Hold `name` for the block if nobody else holds it. Never waits."""

    def reserve(self, key: str, value: str, ttl: timedelta) -> bool:
        """Set `key` only if it is absent. True when this call set it."""

    def put(self, key: str, value: str, ttl: timedelta) -> None: ...

    def get(self, key: str) -> str | None: ...

    def delete(self, key: str) -> None: ...

    def hit(self, key: str, window: timedelta) -> int:
        """Count one event against `key`, whose count lapses after `window`."""

    def reset(self) -> None:
        """Forget every key. The simulation's reset."""


class LocalCoordinator:
    def __init__(self, repository: Repository) -> None:
        self._repo = repository
        self._guard = threading.Lock()
        self._values: dict[str, tuple[str, float]] = {}

    def lock(self, name: str, ttl: timedelta) -> AbstractContextManager[bool]:
        return self._repo.try_lock(name)

    def reserve(self, key: str, value: str, ttl: timedelta) -> bool:
        with self._guard:
            if self._live(key) is not None:
                return False
            self._values[key] = (value, time.monotonic() + ttl.total_seconds())
            return True

    def put(self, key: str, value: str, ttl: timedelta) -> None:
        with self._guard:
            self._values[key] = (value, time.monotonic() + ttl.total_seconds())

    def get(self, key: str) -> str | None:
        with self._guard:
            return self._live(key)

    def delete(self, key: str) -> None:
        with self._guard:
            self._values.pop(key, None)

    def hit(self, key: str, window: timedelta) -> int:
        with self._guard:
            current = self._live(key)
            count = int(current) + 1 if current is not None else 1
            expires = (
                self._values[key][1]
                if current is not None
                else time.monotonic() + window.total_seconds()
            )
            self._values[key] = (str(count), expires)
            return count

    def reset(self) -> None:
        with self._guard:
            self._values = {}

    def _live(self, key: str) -> str | None:
        found = self._values.get(key)
        if found is None:
            return None
        if found[1] <= time.monotonic():
            del self._values[key]
            return None
        return found[0]


# Delete the lock only if this holder still owns it, so a lock that lapsed and was
# taken by someone else is never released by its previous owner.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class RedisCoordinator:
    def __init__(self, client: Any) -> None:
        self._redis = client

    @classmethod
    def from_url(cls, url: str) -> RedisCoordinator:
        import redis

        return cls(redis.Redis.from_url(url, decode_responses=True))

    @contextmanager
    def lock(self, name: str, ttl: timedelta) -> Iterator[bool]:
        key = PREFIX + name
        token = secrets.token_hex(16)
        acquired = bool(self._redis.set(key, token, nx=True, px=_ms(ttl)))
        try:
            yield acquired
        finally:
            if acquired:
                self._redis.eval(_RELEASE, 1, key, token)

    def reserve(self, key: str, value: str, ttl: timedelta) -> bool:
        return bool(self._redis.set(PREFIX + key, value, nx=True, px=_ms(ttl)))

    def put(self, key: str, value: str, ttl: timedelta) -> None:
        self._redis.set(PREFIX + key, value, px=_ms(ttl))

    def get(self, key: str) -> str | None:
        found = self._redis.get(PREFIX + key)
        return None if found is None else str(found)

    def delete(self, key: str) -> None:
        self._redis.delete(PREFIX + key)

    def hit(self, key: str, window: timedelta) -> int:
        count = int(self._redis.incr(PREFIX + key))
        if count == 1:
            self._redis.pexpire(PREFIX + key, _ms(window))
        return count

    def reset(self) -> None:
        for key in self._redis.scan_iter(match=PREFIX + "*"):
            self._redis.delete(key)


def build_coordinator(redis_url: str, repository: Repository) -> Coordinator:
    if redis_url:
        return RedisCoordinator.from_url(redis_url)
    return LocalCoordinator(repository)


def _ms(span: timedelta) -> int:
    return max(1, int(span.total_seconds() * 1000))

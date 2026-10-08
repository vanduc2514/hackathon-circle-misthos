"""Who and what a log line belongs to.

Every request gets a correlation id, from its `X-Request-ID` header when the caller
sent a sensible one and freshly made otherwise, and every log line written while it
is handled carries that id and the actor. The ids live in context variables, which
follow the request into the threadpool, so a money event booked deep in the store
is logged with the id of the request that caused it. The sweeper sets its own id per
pass, so its lines group the same way.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
actor: ContextVar[str | None] = ContextVar("actor", default=None)

_ACCEPTABLE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def new_id() -> str:
    return uuid.uuid4().hex


def accept_or_new(candidate: str | None) -> str:
    """The caller's id if it is short and plain, so it cannot forge log structure."""
    if candidate and _ACCEPTABLE.match(candidate):
        return candidate
    return new_id()


@contextmanager
def bound(correlation: str, who: str | None = None) -> Iterator[None]:
    tokens = (correlation_id.set(correlation), actor.set(who))
    try:
        yield
    finally:
        correlation_id.reset(tokens[0])
        actor.reset(tokens[1])

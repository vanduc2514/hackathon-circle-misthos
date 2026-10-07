"""Guards on the requests that move money or create work.

Idempotency. A client that times out on a request that moves money cannot know
whether the money moved, so it retries, and the retry must not move it again. A
request sent with an `Idempotency-Key` header runs once: the first response is kept
for 24 hours under `idem:{key}` and every retry with the same key gets it back,
marked `Idempotent-Replayed: true`. A retry that arrives while the first is still
running is told so (409) rather than run beside it, and a key reused for a different
request is refused (422). A request that fails keeps nothing, so retrying it runs it.

Rate limits. Per client per minute, one budget for publishing and one for every
other action, counted under `rl:{actor}:{bucket}:{window}`. Over the budget is a 429
with `Retry-After`. The client is its address until accounts exist.

Both live in the store's coordinator: Redis when `MISTHOS_REDIS_URL` is set, so every
API process shares them, and this process's memory otherwise.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from misthos.config import settings
from misthos.store import store

IDEMPOTENCY_TTL = timedelta(hours=24)
# How long a key stays reserved by a request that has not answered yet. Long enough
# for any action to finish, short enough that a process that died mid-request does
# not lock its client out for a day.
PENDING_TTL = timedelta(minutes=2)
MAX_KEY_LENGTH = 255
RATE_WINDOW = timedelta(minutes=1)

REPLAYED = "Idempotent-Replayed"


def actor(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _now() -> float:
    return time.time()


def fingerprint(method: str, path: str, body: bytes) -> str:
    """What makes two requests the same request, for an idempotency key."""
    return hashlib.sha256(b"\n".join([method.encode(), path.encode(), body])).hexdigest()


def rate_limited(bucket: str, per_minute: Callable[[], int]) -> Any:
    """A dependency that counts the request against the client's budget for `bucket`.

    `per_minute` is read on every request so a deployment, or a test, can change it
    without rebuilding the app. Zero turns the limit off.
    """

    async def check(request: Request) -> None:
        budget = per_minute()
        if budget <= 0:
            return
        now = _now()
        window = int(now // RATE_WINDOW.total_seconds())
        key = f"rl:{actor(request)}:{bucket}:{window}"
        count = await run_in_threadpool(store.coordinator.hit, key, RATE_WINDOW)
        if count > budget:
            span = RATE_WINDOW.total_seconds()
            retry_after = max(1, int(span - now % span))
            raise HTTPException(
                status_code=429,
                detail=f"more than {budget} {bucket} requests in a minute; slow down",
                headers={"Retry-After": str(retry_after)},
            )

    return Depends(check)


limit_publish = rate_limited("publish", lambda: settings.rate_limit_publish_per_minute)
limit_actions = rate_limited("actions", lambda: settings.rate_limit_actions_per_minute)
limit_signin = rate_limited("signin", lambda: settings.rate_limit_signin_per_minute)


async def idempotent(
    request: Request,
    key: str | None,
    run: Callable[[], Awaitable[BaseModel]],
    *,
    status_code: int = 200,
) -> Any:
    """Run `run` once per idempotency key, and replay its answer to every retry."""
    if key is None:
        return await run()
    if not key or len(key) > MAX_KEY_LENGTH or not key.isprintable():
        raise HTTPException(
            status_code=400,
            detail=f"Idempotency-Key must be 1 to {MAX_KEY_LENGTH} printable characters",
        )

    this = fingerprint(request.method, request.url.path, await request.body())
    slot = f"idem:{key}"
    pending = json.dumps({"state": "pending", "fingerprint": this})
    coordinator = store.coordinator

    if not await run_in_threadpool(coordinator.reserve, slot, pending, PENDING_TTL):
        kept = await run_in_threadpool(coordinator.get, slot)
        if kept is None:
            # It lapsed between the two calls. Rare enough to ask for a retry.
            raise HTTPException(status_code=409, detail="retry this request")
        found = json.loads(kept)
        if found["fingerprint"] != this:
            raise HTTPException(
                status_code=422,
                detail="this Idempotency-Key was already used for a different request",
            )
        if found["state"] == "pending":
            raise HTTPException(
                status_code=409,
                detail="a request with this Idempotency-Key is still running; retry shortly",
                headers={"Retry-After": "1"},
            )
        return JSONResponse(
            content=found["body"], status_code=found["status"], headers={REPLAYED: "true"}
        )

    try:
        result = await run()
    except BaseException:
        # A failure keeps no answer, so a retry runs again against whatever state the
        # failure left, which is what the client would expect of a request that failed.
        await run_in_threadpool(coordinator.delete, slot)
        raise
    content = result.model_dump(mode="json")
    done = json.dumps(
        {"state": "done", "fingerprint": this, "status": status_code, "body": content}
    )
    await run_in_threadpool(coordinator.put, slot, done, IDEMPOTENCY_TTL)
    return JSONResponse(content=content, status_code=status_code, headers={REPLAYED: "false"})

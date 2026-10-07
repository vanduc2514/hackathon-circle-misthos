"""The GitHub webhook receiver.

Every delivery is verified against the App's webhook secret before anything is read
from it: a receiver that skips that is the easiest way to let anyone post a merge,
and a merge releases money. In production a delivery without a signature is refused,
and so is every delivery while the secret is still the development default. The
simulation accepts unsigned deliveries so the demo can be driven by hand.

Each delivery is handled once. GitHub's delivery id is recorded first, a redelivery
is answered without acting again, and a delivery whose handling failed is forgotten
so that redelivering it from GitHub's settings works.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from misthos.config import settings
from misthos.repositories import StaleIssue
from misthos.services.coordination import Busy
from misthos.services.github import GitHubError
from misthos.services.github.events import HANDLED_EVENTS, Handled, dispatch
from misthos.store import store

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

DEV_SECRET = "dev-secret"
# An action holds its issue for well under a second, so a delivery that finds the
# issue busy waits briefly rather than failing back to GitHub.
BUSY_RETRIES = 5
BUSY_WAIT_SECONDS = 0.2


def signature_for(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@router.get("/github")
async def describe() -> dict[str, object]:
    return {
        "endpoint": "/api/v1/webhooks/github",
        "signature_header": "X-Hub-Signature-256",
        "delivery_header": "X-GitHub-Delivery",
        "handled_events": HANDLED_EVENTS,
        "github": store.github.name,
        "note": "Simulated: no GitHub App is configured."
        if store.github.name == "simulated"
        else "Live: deliveries come from the configured GitHub App.",
    }


def _handle(event: str, payload: dict[str, Any]) -> Handled:
    for attempt in range(BUSY_RETRIES):
        try:
            return dispatch(store, event, payload)
        except (Busy, StaleIssue):
            if attempt == BUSY_RETRIES - 1:
                raise
            time.sleep(BUSY_WAIT_SECONDS)
    raise AssertionError("unreachable")


@router.post("/github", status_code=status.HTTP_202_ACCEPTED)
async def receive(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> dict[str, object]:
    body = await request.body()

    # Verification runs even in simulation when a signature is sent.
    if not settings.simulated:
        if settings.github_webhook_secret == DEV_SECRET:
            raise HTTPException(status_code=503, detail="the webhook secret is not configured")
        if not x_hub_signature_256:
            raise HTTPException(status_code=401, detail="unsigned delivery")
    if x_hub_signature_256 and not hmac.compare_digest(
        signature_for(body, settings.github_webhook_secret), x_hub_signature_256
    ):
        raise HTTPException(status_code=401, detail="signature mismatch")

    event = x_github_event or "unknown"
    try:
        payload = json.loads(body or b"{}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="the body is not JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="the body is not a JSON object")

    receipt: dict[str, object] = {
        "accepted": True,
        "event": event,
        "delivery": x_github_delivery,
        "signature_verified": x_hub_signature_256 is not None,
    }
    if x_github_delivery:
        first = await run_in_threadpool(
            store.repo.record_delivery, x_github_delivery, event, datetime.now(UTC)
        )
        if not first:
            return {**receipt, "duplicate": True, "handled": False, "outcome": "already handled"}

    try:
        handled = await run_in_threadpool(_handle, event, payload)
    except (Busy, StaleIssue, GitHubError) as exc:
        if x_github_delivery:
            await run_in_threadpool(store.repo.forget_delivery, x_github_delivery)
        raise HTTPException(status_code=503, detail=f"retry this delivery: {exc}") from exc
    except Exception:
        if x_github_delivery:
            await run_in_threadpool(store.repo.forget_delivery, x_github_delivery)
        raise

    return {
        **receipt,
        "duplicate": False,
        "handled": handled.handled,
        "outcome": handled.outcome,
        "issue_ids": list(handled.issue_ids),
    }

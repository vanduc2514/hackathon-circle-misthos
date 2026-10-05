from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Header, HTTPException, Request, status

from misthos.config import settings

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# What the receiver would do with each GitHub event, once the App is real.
HANDLED_EVENTS = {
    "issues": "re-price or republish when the issue body changes",
    "pull_request": "move the issue to IN_REVIEW and start the review draft",
    "check_run": "record whether the project's own tests passed",
    "installation": "provision a publisher or contributor identity",
}


@router.get("/github")
async def describe() -> dict[str, object]:
    return {
        "endpoint": "/api/v1/webhooks/github",
        "signature_header": "X-Hub-Signature-256",
        "handled_events": HANDLED_EVENTS,
        "note": "Simulated. No GitHub App is installed in this build.",
    }


@router.post("/github", status_code=status.HTTP_202_ACCEPTED)
async def receive(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict[str, object]:
    body = await request.body()

    # Verification runs even in simulation. A webhook receiver that skips this is
    # the easiest way to let anyone post money-moving events into the system.
    if x_hub_signature_256:
        expected = "sha256=" + hmac.new(
            settings.github_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="signature mismatch")

    return {
        "accepted": True,
        "event": x_github_event or "unknown",
        "signature_verified": x_hub_signature_256 is not None,
        "handled": (x_github_event or "") in HANDLED_EVENTS,
        "bytes": len(body),
    }

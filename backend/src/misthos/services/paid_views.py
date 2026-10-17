"""The paid views over data the platform already holds (#202).

Raw public reads stay free: the newest decisions, a contributor's public profile, the
escrow readback. What an agent pays for is what those do not give: the whole decision
log a page at a time with filters, a contributor's standing taken apart into signals
and specialisation, and a signed certificate of what the escrow holds.

Each function here reads and nothing else. A call that cannot be answered raises
`Unanswerable`, which the route turns into an error the edge does not charge for.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from misthos.domain.escrow import issue_key
from misthos.domain.ledger import EscrowStatus
from misthos.schemas import Account, Decision, money
from misthos.services import certificates, reputation
from misthos.services.chain import ChainUnavailable
from misthos.services.paid import free_tier
from misthos.store import ESCROW, Store

DECISIONS_PAGE_DEFAULT = 50
DECISIONS_PAGE_MAX = 200
"""The most decisions one paid page carries. Documented in docs/PAID-API.md."""

ACTORS = ("agent", "publisher", "contributor", "system")


class Unanswerable(Exception):
    """The call cannot be answered, so it is not charged for."""

    def __init__(self, status: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail


# ------------------------------------------------------------------ who is free


@free_tier("decisions")
def _any_account(account: Account, params: Mapping[str, str]) -> bool:
    """The log is public; a signed-in person exports it free."""
    return True


@free_tier("reputation")
def _own_reputation(account: Account, params: Mapping[str, str]) -> bool:
    return account.role == "contributor" and account.party_id == params.get("contributor_id")


@free_tier("escrow-attestation")
def _own_escrow(account: Account, params: Mapping[str, str]) -> bool:
    if account.role != "publisher":
        return False
    from misthos.store import store

    rec = store.get(params.get("issue_id", ""))
    return rec is not None and rec.publisher_id == account.party_id


# ------------------------------------------------------------------- decisions


def encode_cursor(decision: Decision) -> str:
    raw = f"{decision.created_at.astimezone(UTC).isoformat()}|{decision.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        at, _, decision_id = base64.urlsafe_b64decode(padded.encode()).decode().partition("|")
        moment = datetime.fromisoformat(at)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise Unanswerable(400, "bad_cursor", "the cursor is not one this API gave out") from exc
    if not decision_id or moment.tzinfo is None:
        raise Unanswerable(400, "bad_cursor", "the cursor is not one this API gave out")
    return moment.astimezone(UTC), decision_id


def decisions_page(
    store: Store,
    *,
    cursor: str | None,
    limit: int,
    issue_id: str | None,
    actor: str | None,
    action: str | None,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, Any]:
    if not 1 <= limit <= DECISIONS_PAGE_MAX:
        raise Unanswerable(400, "bad_limit", f"limit is 1 to {DECISIONS_PAGE_MAX}")
    if actor is not None and actor not in ACTORS:
        raise Unanswerable(400, "bad_filter", f"actor is one of {', '.join(ACTORS)}")
    after = decode_cursor(cursor) if cursor else None
    # A moment with no zone is read as UTC, the zone every decision is stamped in.
    since, until = (m.replace(tzinfo=UTC) if m and m.tzinfo is None else m for m in (since, until))
    # One more than the page, to know whether there is a next one.
    rows = store.repo.page_decisions(
        after=after,
        limit=limit + 1,
        issue_id=issue_id,
        actor=actor,
        action=action,
        since=since,
        until=until,
    )
    page, more = rows[:limit], len(rows) > limit
    return {
        "decisions": [d.model_dump(mode="json") for d in page],
        "next": encode_cursor(page[-1]) if more else None,
        "limit": limit,
        "max_page_size": DECISIONS_PAGE_MAX,
        "order": "oldest first",
    }


# ------------------------------------------------------------------ reputation


def reputation_of(store: Store, contributor_id: str) -> dict[str, Any]:
    if store.repo.get_contributor(contributor_id) is None:
        raise Unanswerable(404, "no_such_contributor", f"no contributor {contributor_id}")
    found = reputation.profile(store.repo.list_issues(), contributor_id)
    signals = found.signals
    return {
        "contributor_id": contributor_id,
        "score": found.score,
        "percentile": found.percentile,
        "ranked_contributors": found.ranked,
        "reputation": found.standing.reputation,
        "settled_issues": found.standing.settled_issues,
        "earned": money(found.standing.earned),
        "signals": {
            "issues_held": signals.issues_held,
            "verdicts": {
                "accept": signals.accepted,
                "rework": signals.reworked,
                "reject": signals.rejected,
            },
            "acceptance_rate": (
                round(signals.accepted / signals.verdicts, 3) if signals.verdicts else None
            ),
            "rework_rounds": signals.rework_rounds,
            "first_pass_settled": signals.first_pass_settled,
            "disputes": {
                "opened": signals.disputes_opened,
                "overturned": signals.disputes_overturned,
                "upheld": signals.disputes_upheld,
            },
        },
        "repos": [
            {"repo": r.repo, "settled_issues": r.settled_issues, "earned": money(r.earned)}
            for r in found.repos
        ],
        "labels": [{"label": label, "settled_issues": n} for label, n in found.labels],
        "first_settled_at": _iso(found.first_settled_at),
        "last_settled_at": _iso(found.last_settled_at),
        "method": {
            "points": (
                f"{reputation.POINTS_PER_SETTLED_ISSUE} per settled issue, plus "
                f"{reputation.POINTS_PER_HUNDRED_USDC} per 100 USDC received, at most "
                f"{reputation.MAX_SIZE_POINTS} for size"
            ),
            "score": f"100 * points / (points + {reputation.HALF_SCORE_POINTS})",
            "source": "the money ledger's releases and the decision log, nothing else",
        },
    }


# ------------------------------------------------------------- escrow attestation

_STATUS_OF = {
    EscrowStatus.HELD: "held",
    EscrowStatus.RELEASED: "released",
    EscrowStatus.REFUNDED: "refunded",
}


def escrow_attestation(store: Store, issue_id: str, now: datetime | None = None) -> dict[str, Any]:
    """A signed certificate of what the escrow holds for an issue.

    Read from the chain, compared with the platform's record of the commitment, and
    signed only when the two agree. A divergence is not certified: it is answered 409
    and, like every refusal here, not charged.
    """
    rec = store.get(issue_id)
    if rec is None:
        raise Unanswerable(404, "no_such_issue", f"no issue {issue_id}")
    if rec.escrow is None:
        raise Unanswerable(404, "no_commitment", f"{issue_id} has no escrow commitment")
    try:
        held = store.chain.commitment(issue_id)
    except ChainUnavailable as exc:
        raise Unanswerable(502, "chain_unavailable", "the escrow could not be read") from exc
    if held is None:
        raise Unanswerable(409, "escrow_diverges", "the escrow holds nothing for this issue")

    recorded = int(rec.escrow.amount["base_units"])
    expected_status = (
        "released" if rec.escrow.released else "refunded" if rec.escrow.refunded else "held"
    )
    status = _STATUS_OF[held.status]
    differences = []
    if held.amount.base_units != recorded:
        differences.append("amount")
    if status != expected_status:
        differences.append("status")
    publisher = store.get_publisher(rec.publisher_id)
    if held.publisher and publisher and held.publisher.lower() != publisher.wallet.address.lower():
        differences.append("publisher")
    if held.deadline and held.deadline != rec.escrow.deadline:
        differences.append("deadline")
    if differences:
        raise Unanswerable(
            409,
            "escrow_diverges",
            f"the escrow and the record disagree on {', '.join(differences)}",
        )

    certificate = {
        "kind": "misthos/escrow-attestation",
        "version": 1,
        "issue": {
            "id": rec.id,
            "key": issue_key(rec.id),
            "repo": rec.repo,
            "number": rec.number,
        },
        "publisher": {
            "id": rec.publisher_id,
            "wallet": held.publisher or (publisher.wallet.address if publisher else None),
        },
        "amount": money(held.amount),
        "fee_bps": held.fee_bps,
        "deadline": _iso(held.deadline or rec.escrow.deadline),
        "status": status,
        "escrow": {
            "contract": ESCROW.address,
            "chain_id": ESCROW.chain_id,
            "network": f"eip155:{ESCROW.chain_id}",
            "commit_tx": rec.escrow.tx_hash,
        },
        "read_from": "chain",
        "read_at": _iso(now or datetime.now(UTC)),
    }
    try:
        signed = certificates.sign(certificate)
    except certificates.CertificatesUnavailable as exc:
        raise Unanswerable(503, "certificates_unavailable", "no certificate key") from exc
    return {
        "certificate": certificate,
        "canonical": signed.canonical,
        "signature": signed.signature,
        "signer": signed.signer,
        "scheme": certificates.SCHEME,
        "verify": (
            "recover the signer from `canonical` and `signature` (EIP-191 personal_sign), "
            "compare it with the published signer, then read the commitment from "
            "`escrow.contract` on `escrow.chain_id` by `issue.key`"
        ),
    }


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment else None

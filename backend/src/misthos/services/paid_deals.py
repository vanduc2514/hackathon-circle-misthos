"""Deal flow and settlement artefacts for agents (#206): opportunities, receipts, and a
screening check that stays off until it is cleared.

`opportunities` is the funded work open to a contributor's agent, a page at a time.
The web app's issue list stays free; this is the filtered, paged feed.

`receipt` is a signed record of one issue's settlement: what the escrow released or
refunded, the platform's fee and what the contributor netted, checked against the
money ledger, the issue's record and the escrow before it is signed. The release
transfer names the contributor's wallet on chain, so it is only in the receipt the
contributor reads, free, signed in; everyone else's receipt leaves it out, because
the transfer next to an issue whose contributor is public would link a wallet to a
person (docs/PRIVACY.md).

`screen` checks a wallet against the sanctions list. It is off unless
`MISTHOS_PAID_SCREENING_ENABLED` is set, which waits for the provider's terms and a
legal review; until then it answers 503 and nothing is charged.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from misthos.config import settings
from misthos.domain.escrow import issue_key
from misthos.domain.issue import IssueState
from misthos.domain.ledger import EscrowStatus, MoneyEventKind
from misthos.domain.money import Usdc
from misthos.models.records import IssueRecord
from misthos.schemas import Account, money
from misthos.services import certificates
from misthos.services.chain import ChainUnavailable
from misthos.services.paid import PaidCaller, free_tier
from misthos.services.paid_views import Unanswerable
from misthos.store import ESCROW, Store

OPEN = (IssueState.FUNDED, IssueState.CLAIMED, IssueState.IN_REVIEW, IssueState.REWORK)
"""Funded and not finished: the money is held and the work is not yet accepted."""
OPPORTUNITIES_PAGE_DEFAULT = 25
OPPORTUNITIES_PAGE_MAX = 100
OPPORTUNITY_FILTERS = (
    "repo",
    "labels",
    "min_price",
    "max_price",
    "claimable",
    "sort",
    "cursor",
    "limit",
)
SORTS = ("newest", "price", "deadline")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


@free_tier("opportunities")
def _anyone_signed_in(account: Account, params: Mapping[str, str]) -> bool:
    """The human web view is free, and so is this to anyone signed in."""
    return True


@free_tier("receipt")
def _a_party_to_it(account: Account, params: Mapping[str, str]) -> bool:
    from misthos.store import store

    rec = store.get(params.get("settlement_id", ""))
    if rec is None:
        return False
    if account.role == "contributor":
        return rec.contributor_id == account.party_id
    return account.role == "publisher" and rec.publisher_id == account.party_id


# --------------------------------------------------------------- opportunities


def _usdc(query: Mapping[str, str], name: str) -> Usdc | None:
    if name not in query:
        return None
    try:
        value = Usdc.from_decimal(Decimal(query[name]))
    except (InvalidOperation, ValueError) as exc:
        raise Unanswerable(400, "bad_filter", f"{name} is a USDC amount, such as 250") from exc
    if value.base_units < 0:
        raise Unanswerable(400, "bad_filter", f"{name} is not negative")
    return value


def _committed(rec: IssueRecord) -> Usdc:
    assert rec.escrow is not None
    return Usdc(int(rec.escrow.amount["base_units"]))


def opportunities_page(store: Store, query: Mapping[str, str]) -> dict[str, Any]:
    unknown = sorted(set(query) - set(OPPORTUNITY_FILTERS))
    if unknown:
        raise Unanswerable(
            400,
            "unknown_filter",
            f"unknown {', '.join(unknown)}; this route takes {', '.join(OPPORTUNITY_FILTERS)}",
        )
    try:
        limit = int(query.get("limit", OPPORTUNITIES_PAGE_DEFAULT))
    except ValueError as exc:
        raise Unanswerable(400, "bad_limit", f"limit is 1 to {OPPORTUNITIES_PAGE_MAX}") from exc
    if not 1 <= limit <= OPPORTUNITIES_PAGE_MAX:
        raise Unanswerable(400, "bad_limit", f"limit is 1 to {OPPORTUNITIES_PAGE_MAX}")
    sort = query.get("sort", "newest")
    if sort not in SORTS:
        raise Unanswerable(400, "bad_filter", f"sort is one of {', '.join(SORTS)}")
    claimable = query.get("claimable")
    if claimable not in (None, "true", "false"):
        raise Unanswerable(400, "bad_filter", "claimable is true or false")
    low, high = _usdc(query, "min_price"), _usdc(query, "max_price")
    repo = query.get("repo", "").strip().lower() or None
    labels = {
        label.strip().lower() for label in query.get("labels", "").split(",") if label.strip()
    }
    offset = _offset(query.get("cursor"))

    found = [
        rec
        for rec in store.repo.list_issues(set(OPEN))
        if rec.escrow is not None
        and not rec.escrow.released
        and not rec.escrow.refunded
        and (repo is None or rec.repo.lower() == repo)
        and labels <= {label.lower() for label in rec.labels}
        and (low is None or _committed(rec).base_units >= low.base_units)
        and (high is None or _committed(rec).base_units <= high.base_units)
        and (claimable is None or (rec.state is IssueState.FUNDED) == (claimable == "true"))
    ]
    if sort == "price":
        found.sort(key=lambda r: (-_committed(r).base_units, r.id))
    elif sort == "deadline":
        found.sort(key=lambda r: (r.escrow.deadline if r.escrow else datetime.max, r.id))
    else:
        found.sort(key=lambda r: (r.created_at, r.id), reverse=True)

    page = found[offset : offset + limit]
    return {
        "opportunities": [_opportunity(rec) for rec in page],
        "total": len(found),
        "next": _cursor(offset + limit) if offset + limit < len(found) else None,
        "limit": limit,
        "max_page_size": OPPORTUNITIES_PAGE_MAX,
        "sort": sort,
    }


def _opportunity(rec: IssueRecord) -> dict[str, Any]:
    assert rec.escrow is not None
    amount = _committed(rec)
    fee = Usdc(amount.base_units * rec.escrow.fee_bps // 10_000)
    return {
        "issue_id": rec.id,
        "repo": rec.repo,
        "number": rec.number,
        "title": rec.title,
        "labels": list(rec.labels),
        "github_url": rec.github_url,
        "state": rec.state.value,
        "claimable": rec.state is IssueState.FUNDED,
        "claim": (
            {"claimed": True, "expires_at": rec.claim.expires_at.astimezone(UTC).isoformat()}
            if rec.claim and rec.claim.active
            else {"claimed": False, "expires_at": None}
        ),
        "price": money(amount),
        "contributor_receives": money(amount - fee),
        "fee_bps": rec.escrow.fee_bps,
        "deadline": rec.escrow.deadline.astimezone(UTC).isoformat(),
        "acceptance_criteria": list(rec.acceptance_criteria),
        "compliance_driven": rec.compliance_driven,
    }


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode().rstrip("=")


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode()).decode()
        kind, _, value = raw.partition(":")
        offset = int(value)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise Unanswerable(400, "bad_cursor", "the cursor is not one this API gave out") from exc
    if kind != "offset" or offset < 0:
        raise Unanswerable(400, "bad_cursor", "the cursor is not one this API gave out")
    return offset


# --------------------------------------------------------------------- receipts


def receipt(
    store: Store, settlement_id: str, caller: PaidCaller, now: datetime | None = None
) -> dict[str, Any]:
    """A signed receipt for one issue's settlement. `settlement_id` is the issue's id:
    an issue settles once, by a release or a refund."""
    rec = store.get(settlement_id)
    if rec is None:
        raise Unanswerable(404, "no_such_settlement", f"no settlement {settlement_id}")
    if rec.state not in (IssueState.PAID, IssueState.REFUNDED) or rec.escrow is None:
        raise Unanswerable(404, "not_settled", f"{settlement_id} has not settled")
    released = rec.state is IssueState.PAID
    kind = MoneyEventKind.RELEASED if released else MoneyEventKind.REFUNDED
    committed = [e for e in rec.money_events if e.kind is MoneyEventKind.COMMITTED]
    settled = [e for e in rec.money_events if e.kind is kind]
    gross = _committed(rec)

    differences = []
    if len(committed) != 1 or committed[0].amount != gross:
        differences.append("the commitment")
    if len(settled) != 1 or settled[0].amount != gross:
        differences.append("the settlement")
    if released and (
        rec.paid is None or rec.platform_fee is None or rec.paid + rec.platform_fee != gross
    ):
        differences.append("the fee and the payout")
    try:
        held = store.chain.commitment(rec.id)
    except ChainUnavailable as exc:
        raise Unanswerable(502, "chain_unavailable", "the escrow could not be read") from exc
    expected = EscrowStatus.RELEASED if released else EscrowStatus.REFUNDED
    if held is None or held.status is not expected or held.amount != gross:
        differences.append("the escrow")
    if differences:
        raise Unanswerable(
            409,
            "ledger_diverges",
            f"the ledger and the escrow disagree on {', '.join(differences)}",
        )

    event = settled[0]
    fee = rec.platform_fee if released and rec.platform_fee is not None else Usdc(0)
    net = rec.paid if released and rec.paid is not None else gross
    to_contributor = released and caller.free_caller == f"contributor:{rec.contributor_id}"
    certificate: dict[str, Any] = {
        "kind": "misthos/settlement-receipt",
        "version": 1,
        "settlement_id": rec.id,
        "issue": {"id": rec.id, "key": issue_key(rec.id), "repo": rec.repo, "number": rec.number},
        "outcome": "released" if released else "refunded",
        "gross": money(gross),
        "fee": money(fee),
        "fee_bps": held.fee_bps if held is not None else rec.escrow.fee_bps,
        "net": money(net),
        "to": "contributor" if released else "publisher",
        "settled_at": event.occurred_at.astimezone(UTC).isoformat(),
        "escrow": {
            "contract": ESCROW.address,
            "chain_id": ESCROW.chain_id,
            "network": f"eip155:{ESCROW.chain_id}",
            "commit_tx": rec.escrow.tx_hash,
            # The release names the contributor's wallet: theirs alone to see.
            "settlement_tx": event.tx_hash if (not released or to_contributor) else None,
        },
        "verified_against": ["money ledger", "issue record", "escrow"],
        "issued_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
    }
    try:
        signed = certificates.sign(certificate)
    except certificates.CertificatesUnavailable as exc:
        raise Unanswerable(503, "certificates_unavailable", "no certificate key") from exc
    return {
        "receipt": certificate,
        "canonical": signed.canonical,
        "signature": signed.signature,
        "signer": signed.signer,
        "scheme": certificates.SCHEME,
    }


# -------------------------------------------------------------------- screening


def screen(store: Store, body: Any) -> dict[str, Any]:
    if not settings.paid_screening_enabled:
        raise Unanswerable(
            503,
            "screening_unavailable",
            "sanctions screening is off until its provider terms and a legal review clear it",
        )
    if not isinstance(body, dict) or set(body) - {"address"} or "address" not in body:
        raise Unanswerable(400, "bad_request", 'the body is {"address": "0x…"}')
    address = str(body["address"])
    if not _ADDRESS.match(address):
        raise Unanswerable(400, "bad_address", "address is a 0x EVM address")
    outcome, list_name = store.screening.screen(address)
    if outcome.value == "unavailable":
        raise Unanswerable(503, "screening_unavailable", "the sanctions list could not be read")
    return {
        "address": address,
        "outcome": outcome.value,
        "list": list_name,
        "provider": getattr(store.screening, "name", "screening"),
        "checked_at": datetime.now(UTC).isoformat(),
        "note": "A sanctions list check, not identity verification or legal advice.",
    }

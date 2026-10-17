"""The paid pricing APIs: a price for an issue, and the settled work behind it (#203).

`price` runs the engine the issue page runs, on an issue the caller describes, and
answers what it would suggest: the recommendation, the range, how confident it is
and why, the signals it read, every comparable that moved it, and the justification.
The same inputs give the same answer as `Store.price_advice`, because that is all it
calls. It lists nothing and writes nothing but the quote itself, kept with what was
asked for audit and calibration.

`comparables` lists the settled history the engine compares against, filtered by
repository, labels and signals, a page at a time.

Neither carries a publisher's budget or spending policy, and neither proxies an
issue: the report is the product, not the public issue behind it.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from misthos.domain import comparables as history
from misthos.domain.paid import PriceQuote
from misthos.domain.pricing import WEIGHTS, ComplexitySignals, effort, suggested_range
from misthos.domain.repo import normalize_repo
from misthos.domain.signals import IssueFacts
from misthos.schemas import Account, money
from misthos.services.paid import PaidCaller, free_tier
from misthos.services.paid_views import Unanswerable
from misthos.store import Store

SIGNALS = tuple(WEIGHTS)
COMPARABLES_PAGE_DEFAULT = 20
COMPARABLES_PAGE_MAX = 100
SHOWN_COMPARABLES = 25
"""The most comparables a price report lists. Every one that moved the price is
counted in `comparables_used`, listed or not."""


@free_tier("price")
def _publisher_preview(account: Account, params: Mapping[str, str]) -> bool:
    """A signed-in publisher prices for free; it is the preview the web app gives."""
    return account.role == "publisher"


@free_tier("comparables")
def _publisher_comparables(account: Account, params: Mapping[str, str]) -> bool:
    return account.role == "publisher"


class PriceAsked(BaseModel):
    """An issue to price, as the caller describes it."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=20_000)
    repo: str = Field(min_length=3, max_length=200)
    labels: list[str] = Field(default_factory=list, max_length=20)
    signals: dict[str, float] = Field(default_factory=dict)
    """Any of the six, 1 to 5, set over what the engine reads from the issue."""
    compliance_driven: bool = False
    prior_attempts: int = Field(default=0, ge=0, le=100)
    """Pull requests that tried the issue and were closed without merging."""

    @field_validator("labels")
    @classmethod
    def _labels(cls, labels: list[str]) -> list[str]:
        cleaned = [label.strip() for label in labels]
        if any(not label or len(label) > 50 for label in cleaned):
            raise ValueError("a label is 1 to 50 characters")
        return cleaned


def parse_price_asked(body: Any) -> PriceAsked:
    try:
        asked = PriceAsked.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "body"
        raise Unanswerable(400, "bad_request", f"{where}: {first.get('msg', 'invalid')}") from exc
    unknown = sorted(set(asked.signals) - set(SIGNALS))
    if unknown:
        raise Unanswerable(
            400,
            "bad_signals",
            f"unknown signal {', '.join(unknown)}; the signals are {', '.join(SIGNALS)}",
        )
    for name, value in asked.signals.items():
        if not 1.0 <= value <= 5.0:
            raise Unanswerable(400, "bad_signals", f"signal {name} is 1 to 5, not {value:g}")
    return asked


def _confidence_reason(found: list[history.Comparable], confidence: str) -> str:
    if not found:
        return (
            "No settled issue of similar shape in the last year, so the range rests on the "
            "signals alone. Treat it as a first guess."
        )
    if confidence == "high":
        return f"{len(found)} settled issues of similar shape agree on what it is worth."
    if len(found) < history.HIGH_CONFIDENCE_AT:
        return f"Only {len(found)} settled issue(s) of similar shape: evidence, not yet a market."
    return f"{len(found)} settled issues of similar shape disagree about what it is worth."


def price(
    store: Store, asked: PriceAsked, caller: PaidCaller, request_digest: str | None
) -> dict[str, Any]:
    repo = normalize_repo(asked.repo)
    if not repo:
        raise Unanswerable(400, "bad_repo", "repo is owner/name or a github.com link")
    facts = IssueFacts(
        repo=repo,
        number=0,
        title=asked.title,
        body=asked.body,
        labels=tuple(asked.labels),
        prior_attempts=asked.prior_attempts,
    )
    reading, chosen, proposal, found = store.price_advice(
        facts, signals=asked.signals, compliance_driven=asked.compliance_driven
    )
    low, high = suggested_range(proposal)
    report = {
        "repo": repo,
        "recommended": money(proposal.recommended),
        "band": {"low": money(low), "high": money(high)},
        "confidence": proposal.confidence,
        "confidence_reason": _confidence_reason(found, proposal.confidence),
        "complexity_score": proposal.complexity_score,
        "estimated_hours": proposal.estimated_hours,
        "signals": {
            name: {
                "score": value,
                "source": "given" if name in asked.signals else "read",
                "reason": None if name in asked.signals else reading.reasons.get(name),
            }
            for name, value in chosen.as_dict().items()
        },
        "comparables_used": len(found),
        "comparables": [_comparable(c, repo) for c in found[:SHOWN_COMPARABLES]],
        "justification": proposal.justification,
        "advice": (
            "A suggestion, not a price: on Misthos the publisher names the price, and "
            "nothing here bounds it."
        ),
    }
    store.repo.record_price_quote(
        PriceQuote(
            id=f"PQ-{store.repo.next_value('price_quote', 1):06d}",
            route="price",
            request_digest=request_digest,
            payer=caller.payer.lower() if caller.payer else None,
            free_caller=caller.free_caller,
            inputs=asked.model_dump(mode="json") | {"repo": repo},
            output=report,
            quoted_at=datetime.now(UTC),
        )
    )
    return report


def _comparable(c: history.Comparable, repo: str | None) -> dict[str, Any]:
    return {
        "issue_id": c.work.issue_id,
        "repo": c.work.repo,
        "title": c.work.title,
        "price": money(c.work.price),
        "settled_at": c.work.settled_at.astimezone(UTC).isoformat(),
        "distance": c.distance,
        "implied": money(c.implied),
        "same_repo": bool(repo) and c.work.repo.lower() == (repo or "").lower(),
    }


# ----------------------------------------------------------------- comparables


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


COMPARABLES_FILTERS = ("repo", "labels", "cursor", "limit", *SIGNALS)


def comparables_page(store: Store, query: Mapping[str, str]) -> dict[str, Any]:
    """Settled work, filtered by repository, labels and signals, a page at a time.

    With all six signals, the history is the engine's own comparables for an issue of
    that shape: within a year and close enough to count, closest first (same-repository
    work ahead), each with the price it implies. Without signals it is every settlement,
    newest first. Some signals but not all is refused: a distance needs all six.
    """
    unknown = sorted(set(query) - set(COMPARABLES_FILTERS))
    if unknown:
        raise Unanswerable(
            400,
            "unknown_filter",
            f"unknown {', '.join(unknown)}; this route takes {', '.join(COMPARABLES_FILTERS)}",
        )
    try:
        limit = int(query.get("limit", COMPARABLES_PAGE_DEFAULT))
    except ValueError as exc:
        raise Unanswerable(400, "bad_limit", f"limit is 1 to {COMPARABLES_PAGE_MAX}") from exc
    if not 1 <= limit <= COMPARABLES_PAGE_MAX:
        raise Unanswerable(400, "bad_limit", f"limit is 1 to {COMPARABLES_PAGE_MAX}")
    offset = _offset(query.get("cursor"))

    repo = None
    if "repo" in query:
        repo = normalize_repo(query["repo"])
        if not repo:
            raise Unanswerable(400, "bad_repo", "repo is owner/name or a github.com link")
    labels = {
        label.strip().lower() for label in query.get("labels", "").split(",") if label.strip()
    }

    given = {name: query[name] for name in SIGNALS if name in query}
    signals: dict[str, float] | None = None
    if given:
        missing = [name for name in SIGNALS if name not in given]
        if missing:
            raise Unanswerable(
                400, "bad_signals", f"give all six signals or none; missing {', '.join(missing)}"
            )
        try:
            signals = {name: float(value) for name, value in given.items()}
        except ValueError as exc:
            raise Unanswerable(400, "bad_signals", "a signal is a number, 1 to 5") from exc
        bad = [name for name, value in signals.items() if not 1.0 <= value <= 5.0]
        if bad:
            raise Unanswerable(400, "bad_signals", f"{', '.join(bad)} must be 1 to 5")

    labels_of = {
        rec.id: {label.lower() for label in rec.labels} for rec in store.repo.list_issues()
    }
    settled = [
        w
        for w in store.settled_history()
        if (repo is None or w.repo.lower() == repo.lower())
        and labels <= labels_of.get(w.issue_id, set())
    ]
    if signals is not None:
        target = ComplexitySignals(**signals)
        rows = [
            _comparable(c, repo)
            for c in history.find(
                target.as_dict(),
                effort(target),
                settled,
                weights=WEIGHTS,
                now=datetime.now(UTC),
                repo=repo,
            )
        ]
    else:
        rows = [
            {
                "issue_id": w.issue_id,
                "repo": w.repo,
                "title": w.title,
                "price": money(w.price),
                "settled_at": w.settled_at.astimezone(UTC).isoformat(),
                "distance": None,
                "implied": None,
                "same_repo": bool(repo),
            }
            for w in sorted(settled, key=lambda w: (w.settled_at, w.issue_id), reverse=True)
        ]
    page = rows[offset : offset + limit]
    more = offset + limit < len(rows)
    return {
        "comparables": page,
        "total": len(rows),
        "next": _cursor(offset + limit) if more else None,
        "limit": limit,
        "max_page_size": COMPARABLES_PAGE_MAX,
        "order": "closest first" if signals is not None else "newest first",
        "window_days": history.WINDOW.days if signals is not None else None,
    }

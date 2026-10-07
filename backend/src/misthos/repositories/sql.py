"""SQL repository: Postgres in a deployment, a SQLite file for durable local runs.

An issue is saved whole inside one transaction, guarded by a version column: the
update only lands when the stored version is the one this copy was loaded at. Child
rows follow the schema's rules. Proposals, submissions, verdicts and decisions are
only ever inserted, the commitment mirror and the claim are updated in place, and an
issue's current proposal, claim, submission and verdict are its newest rows.

The schema is brought to the latest migration before first use, so a fresh database
needs no separate setup step and a deployment cannot run against an old schema.
"""

from __future__ import annotations

import threading
import zlib
from collections import defaultdict
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, create_engine, delete, func, insert, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from misthos.domain.comparables import ComparableRef
from misthos.domain.compliance import (
    PartyKind,
    Screening,
    ScreeningOutcome,
    ScreeningReason,
)
from misthos.domain.issue import IssueState
from misthos.domain.ledger import MoneyEvent, MoneyEventKind
from misthos.domain.money import Usdc, format_usdc
from misthos.domain.pricing import PriceProposal
from misthos.models import tables as t
from misthos.models.records import IssueRecord
from misthos.repositories.base import (
    AccountConflict,
    AppendOnlyViolation,
    PaymentAlreadyUsed,
    StaleIssue,
)
from misthos.schemas import (
    Account,
    Claim,
    Contributor,
    Decision,
    EscrowCommitment,
    PaymentRequest,
    Publisher,
    Review,
    Submission,
    Subscription,
    SubscriptionPayment,
    Wallet,
    money,
)

Row = Mapping[str, Any]


def normalise_url(url: str) -> str:
    """Point a bare postgres URL at psycopg 3, the driver this package installs."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


class SqlRepository:
    def __init__(self, url: str) -> None:
        self.url = normalise_url(url)
        self.engine: Engine = create_engine(self.url, pool_pre_ping=True)
        self._schema_ready = False
        self._schema_guard = threading.Lock()
        self._local_locks: dict[str, threading.Lock] = {}

    # -------------------------------------------------------------- schema

    def migrate(self) -> None:
        """Upgrade to the latest migration once per process."""
        if self._schema_ready:
            return
        with self._schema_guard:
            if not self._schema_ready:
                from misthos.migrations import upgrade_to_head

                upgrade_to_head(self.engine)
                self._schema_ready = True

    def reset(self) -> None:
        self.migrate()
        with self.engine.begin() as conn:
            for table in reversed(t.metadata.sorted_tables):
                conn.execute(delete(table))

    def is_empty(self) -> bool:
        self.migrate()
        with self.engine.connect() as conn:
            publishers = conn.execute(select(func.count()).select_from(t.publishers))
            return publishers.scalar_one() == 0

    def next_value(self, counter: str, start: int) -> int:
        self.migrate()
        with self.engine.begin() as conn:
            bumped = conn.execute(
                update(t.counters)
                .where(t.counters.c.name == counter)
                .values(value=t.counters.c.value + 1)
                .returning(t.counters.c.value)
            ).first()
            if bumped is not None:
                return int(bumped[0])
            conn.execute(insert(t.counters).values(name=counter, value=start))
            return start

    # ------------------------------------------------------------- parties

    def save_publisher(self, publisher: Publisher) -> None:
        self.migrate()
        values = {
            "name": publisher.name,
            "kind": publisher.kind,
            "tier": publisher.tier,
            "wallet_address": publisher.wallet.address,
            "chain": publisher.wallet.chain,
            "budget_remaining_base_units": _parse_usdc(publisher.budget_remaining_usdc).base_units,
            "approval_threshold_base_units": (
                _parse_usdc(publisher.approval_threshold_usdc).base_units
                if publisher.approval_threshold_usdc is not None
                else None
            ),
            "approvers": list(publisher.approvers),
            "category_limits": {
                label: _parse_usdc(limit).base_units
                for label, limit in publisher.category_limits.items()
            },
        }
        with self.engine.begin() as conn:
            _upsert(conn, t.publishers, {"id": publisher.id}, values)

    def get_publisher(self, publisher_id: str) -> Publisher | None:
        self.migrate()
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(t.publishers).where(t.publishers.c.id == publisher_id))
                .mappings()
                .first()
            )
        return _publisher(row) if row else None

    def list_publishers(self) -> list[Publisher]:
        self.migrate()
        with self.engine.connect() as conn:
            rows = conn.execute(select(t.publishers).order_by(t.publishers.c.id)).mappings()
            return [_publisher(r) for r in rows]

    def save_contributor(self, contributor: Contributor) -> None:
        self.migrate()
        values = {
            "handle": contributor.handle,
            "wallet_address": contributor.wallet.address,
            "chain": contributor.wallet.chain,
            "reputation": contributor.reputation,
            "settled_issues": contributor.settled_issues,
            "earned_base_units": _parse_usdc(contributor.earned_usdc).base_units,
            "identity_status": contributor.identity_status,
            "identity_reference": contributor.identity_reference,
            "identity_verified_at": contributor.identity_verified_at,
        }
        with self.engine.begin() as conn:
            _upsert(conn, t.contributors, {"id": contributor.id}, values)

    def get_contributor(self, contributor_id: str) -> Contributor | None:
        self.migrate()
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(t.contributors).where(t.contributors.c.id == contributor_id))
                .mappings()
                .first()
            )
        return _contributor(row) if row else None

    def list_contributors(self) -> list[Contributor]:
        self.migrate()
        with self.engine.connect() as conn:
            rows = conn.execute(select(t.contributors).order_by(t.contributors.c.id)).mappings()
            return [_contributor(r) for r in rows]

    # ---------------------------------------------------------- screenings

    def record_screening(self, screening: Screening) -> None:
        self.migrate()
        with self.engine.begin() as conn:
            conn.execute(
                insert(t.screenings).values(
                    party_kind=screening.party_kind.value,
                    party_id=screening.party_id,
                    wallet_address=screening.wallet_address,
                    outcome=screening.outcome.value,
                    list_name=screening.list_name,
                    provider=screening.provider,
                    reason=screening.reason.value,
                    checked_at=screening.checked_at,
                )
            )

    def latest_screening(self, party_kind: PartyKind, party_id: str) -> Screening | None:
        found = self._screenings(party_kind, party_id, newest_first=True, limit=1)
        return found[0] if found else None

    def list_screenings(self, party_kind: PartyKind, party_id: str) -> list[Screening]:
        return self._screenings(party_kind, party_id)

    def _screenings(
        self,
        party_kind: PartyKind,
        party_id: str,
        *,
        newest_first: bool = False,
        limit: int | None = None,
    ) -> list[Screening]:
        self.migrate()
        columns = (t.screenings.c.checked_at, t.screenings.c.id)
        order = [c.desc() for c in columns] if newest_first else list(columns)
        query = (
            select(t.screenings)
            .where(
                t.screenings.c.party_kind == party_kind.value,
                t.screenings.c.party_id == party_id,
            )
            .order_by(*order)
            .limit(limit)
        )
        with self.engine.connect() as conn:
            return [_screening(r) for r in conn.execute(query).mappings()]

    def purge_screenings(self, before: datetime) -> int:
        self.migrate()
        with self.engine.begin() as conn:
            return conn.execute(
                delete(t.screenings).where(t.screenings.c.checked_at < before)
            ).rowcount

    # ------------------------------------------------------------ accounts

    def get_account(self, address: str) -> Account | None:
        self.migrate()
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(t.accounts).where(t.accounts.c.address == address.lower()))
                .mappings()
                .first()
            )
        if row is None:
            return None
        return Account(
            address=row["address"],
            role=row["role"],
            party_id=row["party_id"],
            github_login=row["github_login"],
            created_at=_utc(row["created_at"]),
        )

    def save_account(self, account: Account) -> None:
        self.migrate()
        values = {
            "role": account.role,
            "party_id": account.party_id,
            "github_login": account.github_login,
            "created_at": account.created_at,
        }
        try:
            with self.engine.begin() as conn:
                _upsert(conn, t.accounts, {"address": account.address.lower()}, values)
        except IntegrityError as exc:
            raise AccountConflict(f"{account.github_login} is linked to another wallet") from exc

    # ---------------------------------------------------------- plans

    def get_subscription(self, publisher_id: str) -> Subscription | None:
        self.migrate()
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(t.subscriptions).where(t.subscriptions.c.publisher_id == publisher_id)
                )
                .mappings()
                .first()
            )
        return _subscription(row) if row is not None else None

    def save_subscription(self, subscription: Subscription) -> None:
        self.migrate()
        values = {
            "plan": subscription.plan,
            "status": subscription.status,
            "period_start": subscription.period_start,
            "period_end": subscription.period_end,
            "cancel_at_period_end": subscription.cancel_at_period_end,
            "pending": subscription.pending.model_dump(mode="json")
            if subscription.pending
            else None,
            "updated_at": subscription.updated_at,
        }
        with self.engine.begin() as conn:
            _upsert(conn, t.subscriptions, {"publisher_id": subscription.publisher_id}, values)

    def list_subscriptions(self) -> list[Subscription]:
        self.migrate()
        with self.engine.connect() as conn:
            rows = conn.execute(select(t.subscriptions)).mappings().all()
        return [_subscription(r) for r in rows]

    def add_subscription_payment(self, payment: SubscriptionPayment) -> None:
        self.migrate()
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(t.subscription_payments).values(
                        publisher_id=payment.publisher_id,
                        plan=payment.plan,
                        amount_base_units=Usdc.from_decimal(payment.amount_usdc).base_units,
                        tx_hash=payment.tx_hash.lower(),
                        period_start=payment.period_start,
                        period_end=payment.period_end,
                        paid_at=payment.paid_at,
                    )
                )
        except IntegrityError as exc:
            raise PaymentAlreadyUsed(payment.tx_hash) from exc

    def list_subscription_payments(self, publisher_id: str) -> list[SubscriptionPayment]:
        self.migrate()
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(t.subscription_payments)
                    .where(t.subscription_payments.c.publisher_id == publisher_id)
                    .order_by(t.subscription_payments.c.id)
                )
                .mappings()
                .all()
            )
        return [
            SubscriptionPayment(
                publisher_id=r["publisher_id"],
                plan=r["plan"],
                amount_usdc=str(Usdc(r["amount_base_units"]).decimal),
                tx_hash=r["tx_hash"],
                period_start=_utc(r["period_start"]),
                period_end=_utc(r["period_end"]),
                paid_at=_utc(r["paid_at"]),
            )
            for r in rows
        ]

    # ---------------------------------------------------------- deliveries

    def record_delivery(self, delivery_id: str, event: str, at: datetime) -> bool:
        self.migrate()
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(t.webhook_deliveries).values(
                        delivery_id=delivery_id, event=event, received_at=at
                    )
                )
        except IntegrityError:
            return False
        return True

    def forget_delivery(self, delivery_id: str) -> None:
        self.migrate()
        with self.engine.begin() as conn:
            conn.execute(
                delete(t.webhook_deliveries).where(
                    t.webhook_deliveries.c.delivery_id == delivery_id
                )
            )

    # -------------------------------------------------------------- issues

    def get_issue(self, issue_id: str) -> IssueRecord | None:
        self.migrate()
        with self.engine.connect() as conn:
            rows = conn.execute(select(t.issues).where(t.issues.c.id == issue_id)).mappings().all()
            found = _load(conn, rows)
        return found[0] if found else None

    def list_issues(self, states: Collection[IssueState] | None = None) -> list[IssueRecord]:
        self.migrate()
        query = select(t.issues).order_by(t.issues.c.id)
        if states is not None:
            query = query.where(t.issues.c.state.in_([s.value for s in states]))
        with self.engine.connect() as conn:
            return _load(conn, conn.execute(query).mappings().all())

    def count_issues(self) -> int:
        self.migrate()
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(t.issues)).scalar_one()

    def save_issues(self, *records: IssueRecord) -> None:
        self.migrate()
        with self.engine.begin() as conn:
            for rec in records:
                _save(conn, rec)
        # Only once the transaction has committed do the copies become current.
        for rec in records:
            rec.version += 1

    def list_decisions(self, limit: int) -> list[Decision]:
        self.migrate()
        query = (
            select(t.decisions)
            .order_by(t.decisions.c.created_at.desc(), t.decisions.c.id.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            return [_decision(r) for r in conn.execute(query).mappings()]

    @contextmanager
    def try_lock(self, name: str) -> Iterator[bool]:
        if self.engine.dialect.name != "postgresql":
            # SQLite is a single-process database here, so a process lock is enough.
            lock = self._local_locks.setdefault(name, threading.Lock())
            acquired = lock.acquire(blocking=False)
            try:
                yield acquired
            finally:
                if acquired:
                    lock.release()
            return

        key = zlib.crc32(f"misthos:{name}".encode())
        with self.engine.connect() as conn:
            acquired = bool(
                conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
            )
            try:
                yield acquired
            finally:
                if acquired:
                    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    conn.commit()


# ------------------------------------------------------------------ saving


def _save(conn: Connection, rec: IssueRecord) -> None:
    values = {
        "repo": rec.repo,
        "number": rec.number,
        "title": rec.title,
        "summary": rec.summary,
        "state": rec.state.value,
        "labels": list(rec.labels),
        "compliance_driven": rec.compliance_driven,
        "acceptance_criteria": list(rec.acceptance_criteria),
        "publisher_id": rec.publisher_id,
        "contributor_id": rec.contributor_id,
        "created_at": rec.created_at,
        "deadline": rec.deadline,
        "paid_base_units": rec.paid.base_units if rec.paid else None,
        "paid_at": rec.paid_at,
        "payout_tx_hash": rec.payout_tx_hash,
        "accepted_by": rec.accepted_by,
        "payout_hold": rec.payout_hold,
        "payout_checked_at": rec.payout_checked_at,
        "criteria_approved_at": rec.criteria_approved_at,
        "relisted_from": rec.relisted_from,
    }
    if rec.version == 0:
        exists = conn.execute(select(t.issues.c.id).where(t.issues.c.id == rec.id)).first()
        if exists is not None:
            raise StaleIssue(rec.id)
        conn.execute(insert(t.issues).values(id=rec.id, version=1, **values))
    else:
        written = conn.execute(
            update(t.issues)
            .where(t.issues.c.id == rec.id, t.issues.c.version == rec.version)
            .values(version=rec.version + 1, **values)
        )
        if written.rowcount != 1:
            raise StaleIssue(rec.id)

    _save_proposal(conn, rec)
    _save_escrow(conn, rec)
    _save_claim(conn, rec)
    _save_submission(conn, rec)
    _save_review(conn, rec)
    _save_decisions(conn, rec)
    _save_money_events(conn, rec)


def _save_proposal(conn: Connection, rec: IssueRecord) -> None:
    if rec.proposal is None:
        return
    p = rec.proposal
    values = {
        "band_low_base_units": p.band_low.base_units,
        "band_high_base_units": p.band_high.base_units,
        "recommended_base_units": p.recommended.base_units,
        "estimated_hours": p.estimated_hours,
        "complexity_score": p.complexity_score,
        "confidence": p.confidence,
        "signals": dict(p.signals),
        "justification": p.justification,
        "fundable": p.fundable,
        # None rather than an empty list, so a proposal saved before #42 is unchanged.
        "comparables": [_comparable_row(c) for c in p.comparables] or None,
    }
    latest = (
        conn.execute(
            select(t.price_proposals)
            .where(t.price_proposals.c.issue_id == rec.id)
            .order_by(t.price_proposals.c.seq.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    if latest is not None and all(latest[k] == v for k, v in values.items()):
        return
    # A changed price is a new proposal, never an edit of the old one.
    conn.execute(
        insert(t.price_proposals).values(
            issue_id=rec.id,
            seq=(latest["seq"] + 1) if latest else 1,
            created_at=datetime.now(UTC),
            **values,
        )
    )


def _save_escrow(conn: Connection, rec: IssueRecord) -> None:
    if rec.escrow is None:
        return
    e = rec.escrow
    _upsert(
        conn,
        t.escrow_commitments,
        {"issue_id": rec.id},
        {
            "contract": e.contract,
            "chain": e.chain,
            "tx_hash": e.tx_hash,
            "amount_base_units": int(e.amount["base_units"]),
            "deadline": e.deadline,
            "released": e.released,
            "refunded": e.refunded,
        },
    )


def _save_claim(conn: Connection, rec: IssueRecord) -> None:
    c = rec.claim if rec.claim is not None and rec.claim.active else None
    others = t.claims.c.issue_id == rec.id
    if c is not None:
        others = others & ~(
            (t.claims.c.contributor_id == c.contributor_id) & (t.claims.c.issued_at == c.issued_at)
        )
    # Retire whatever was active before, then record the current claim, so the
    # one-active-claim index never sees two at once.
    conn.execute(update(t.claims).where(others, t.claims.c.active.is_(True)).values(active=False))
    if c is not None:
        _upsert(
            conn,
            t.claims,
            {"issue_id": rec.id, "contributor_id": c.contributor_id, "issued_at": c.issued_at},
            {"expires_at": c.expires_at, "active": True},
        )


def _save_submission(conn: Connection, rec: IssueRecord) -> None:
    s = rec.submission
    if s is None:
        return
    # One row per submitted commit. The project's checks on that commit finish after
    # it is submitted, so the row is updated when their result arrives.
    key = {"issue_id": rec.id, "head_sha": s.head_sha}
    _upsert(conn, t.submissions, key, s.model_dump(exclude={"head_sha"}))


def _save_review(conn: Connection, rec: IssueRecord) -> None:
    r = rec.review
    if r is None:
        return
    key = {"issue_id": rec.id, "decided_at": r.decided_at}
    if _exists(conn, t.reviews, key):
        return
    conn.execute(
        insert(t.reviews).values(
            **key,
            verdict=r.verdict,
            findings=list(r.findings),
            head_sha=r.head_sha,
            reviewer=r.reviewer,
            seconds=r.seconds,
            cost_base_units=(
                Usdc.from_decimal(r.cost_usdc).base_units if r.cost_usdc is not None else None
            ),
        )
    )


def _save_decisions(conn: Connection, rec: IssueRecord) -> None:
    stored = conn.execute(
        select(func.count()).select_from(t.decisions).where(t.decisions.c.issue_id == rec.id)
    ).scalar_one()
    if len(rec.decisions) < stored:
        raise AppendOnlyViolation(f"issue {rec.id} would lose decisions")
    fresh = rec.decisions[stored:]
    if fresh:
        conn.execute(
            insert(t.decisions),
            [
                {
                    "id": d.id,
                    "issue_id": rec.id,
                    "position": stored + offset,
                    "actor": d.actor,
                    "action": d.action,
                    "rule": d.rule,
                    "outcome": d.outcome,
                    "cost_usdc": d.cost_usdc,
                    "created_at": d.created_at,
                }
                for offset, d in enumerate(fresh)
            ],
        )


def _save_money_events(conn: Connection, rec: IssueRecord) -> None:
    stored = conn.execute(
        select(func.count()).select_from(t.money_events).where(t.money_events.c.issue_id == rec.id)
    ).scalar_one()
    if len(rec.money_events) < stored:
        raise AppendOnlyViolation(f"issue {rec.id} would lose money events")
    fresh = rec.money_events[stored:]
    if fresh:
        conn.execute(
            insert(t.money_events),
            [
                {
                    "issue_id": rec.id,
                    "position": stored + offset,
                    "kind": e.kind.value,
                    "amount_base_units": e.amount.base_units,
                    "counterparty_id": e.counterparty_id,
                    "tx_hash": e.tx_hash,
                    "occurred_at": e.occurred_at,
                }
                for offset, e in enumerate(fresh)
            ],
        )


def _upsert(conn: Connection, table: Any, key: dict[str, Any], values: dict[str, Any]) -> None:
    where = [table.c[k] == v for k, v in key.items()]
    if conn.execute(update(table).where(*where).values(**values)).rowcount == 0:
        conn.execute(insert(table).values(**key, **values))


def _exists(conn: Connection, table: Any, key: dict[str, Any]) -> bool:
    where = [table.c[k] == v for k, v in key.items()]
    return conn.execute(select(func.count()).select_from(table).where(*where)).scalar_one() > 0


# ----------------------------------------------------------------- loading


def _load(conn: Connection, issue_rows: Sequence[Row]) -> list[IssueRecord]:
    """Build aggregates for these rows with one query per child table, not per issue."""
    ids = [r["id"] for r in issue_rows]
    if not ids:
        return []

    def newest(table: Any, order: Any, *where: Any) -> dict[str, Row]:
        rows = conn.execute(
            select(table).where(table.c.issue_id.in_(ids), *where).order_by(order)
        ).mappings()
        return {r["issue_id"]: r for r in rows}  # later rows overwrite earlier ones

    proposals = newest(t.price_proposals, t.price_proposals.c.seq)
    escrows = newest(t.escrow_commitments, t.escrow_commitments.c.issue_id)
    claims = newest(t.claims, t.claims.c.issued_at, t.claims.c.active.is_(True))
    submissions = newest(t.submissions, t.submissions.c.id)
    reviews = newest(t.reviews, t.reviews.c.id)

    logs: dict[str, list[Decision]] = defaultdict(list)
    for row in conn.execute(
        select(t.decisions)
        .where(t.decisions.c.issue_id.in_(ids))
        .order_by(t.decisions.c.issue_id, t.decisions.c.position)
    ).mappings():
        logs[row["issue_id"]].append(_decision(row))

    ledger: dict[str, list[MoneyEvent]] = defaultdict(list)
    for row in conn.execute(
        select(t.money_events)
        .where(t.money_events.c.issue_id.in_(ids))
        .order_by(t.money_events.c.issue_id, t.money_events.c.position)
    ).mappings():
        ledger[row["issue_id"]].append(_money_event(row))

    return [
        IssueRecord(
            id=row["id"],
            repo=row["repo"],
            number=row["number"],
            title=row["title"],
            summary=row["summary"],
            state=IssueState(row["state"]),
            labels=list(row["labels"]),
            compliance_driven=row["compliance_driven"],
            acceptance_criteria=list(row["acceptance_criteria"]),
            publisher_id=row["publisher_id"],
            created_at=_utc(row["created_at"]),
            deadline=_utc(row["deadline"]) if row["deadline"] else None,
            proposal=_proposal(proposals[row["id"]]) if row["id"] in proposals else None,
            escrow=_escrow(escrows[row["id"]]) if row["id"] in escrows else None,
            claim=_claim(claims[row["id"]]) if row["id"] in claims else None,
            submission=_submission(submissions[row["id"]]) if row["id"] in submissions else None,
            review=_review(reviews[row["id"]]) if row["id"] in reviews else None,
            contributor_id=row["contributor_id"],
            paid=Usdc(row["paid_base_units"]) if row["paid_base_units"] is not None else None,
            paid_at=_utc_or_none(row["paid_at"]),
            payout_tx_hash=row["payout_tx_hash"],
            accepted_by=row["accepted_by"],
            payout_hold=row["payout_hold"],
            payout_checked_at=_utc_or_none(row["payout_checked_at"]),
            criteria_approved_at=_utc_or_none(row["criteria_approved_at"]),
            relisted_from=row["relisted_from"],
            decisions=logs[row["id"]],
            money_events=ledger[row["id"]],
            version=row["version"],
        )
        for row in issue_rows
    ]


def _utc(moment: datetime) -> datetime:
    # SQLite hands timestamps back without a zone; everything here is stored in UTC.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _utc_or_none(moment: datetime | None) -> datetime | None:
    return _utc(moment) if moment is not None else None


def _parse_usdc(display: str) -> Usdc:
    return Usdc.from_decimal(display.replace(",", ""))


def _publisher(row: Row) -> Publisher:
    return Publisher(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        tier=row["tier"],
        wallet=Wallet(address=row["wallet_address"], chain=row["chain"]),
        budget_remaining_usdc=format_usdc(Usdc(row["budget_remaining_base_units"])),
        approval_threshold_usdc=(
            format_usdc(Usdc(row["approval_threshold_base_units"]))
            if row["approval_threshold_base_units"] is not None
            else None
        ),
        approvers=list(row["approvers"] or []),
        category_limits={
            label: format_usdc(Usdc(int(units)))
            for label, units in (row["category_limits"] or {}).items()
        },
    )


def _contributor(row: Row) -> Contributor:
    return Contributor(
        id=row["id"],
        handle=row["handle"],
        wallet=Wallet(address=row["wallet_address"], chain=row["chain"]),
        reputation=row["reputation"],
        settled_issues=row["settled_issues"],
        earned_usdc=format_usdc(Usdc(row["earned_base_units"])),
        identity_status=row["identity_status"],
        identity_reference=row["identity_reference"],
        identity_verified_at=_utc_or_none(row["identity_verified_at"]),
    )


def _proposal(row: Row) -> PriceProposal:
    return PriceProposal(
        band_low=Usdc(row["band_low_base_units"]),
        band_high=Usdc(row["band_high_base_units"]),
        recommended=Usdc(row["recommended_base_units"]),
        estimated_hours=row["estimated_hours"],
        complexity_score=row["complexity_score"],
        confidence=row["confidence"],
        signals=dict(row["signals"]),
        justification=row["justification"],
        fundable=row["fundable"],
        comparables=tuple(_comparable(c) for c in row["comparables"] or []),
    )


def _comparable_row(ref: ComparableRef) -> dict:
    return {
        "issue_id": ref.issue_id,
        "repo": ref.repo,
        "title": ref.title,
        "price_base_units": ref.price.base_units,
        "settled_at": ref.settled_at.isoformat(),
        "distance": ref.distance,
    }


def _comparable(row: dict) -> ComparableRef:
    return ComparableRef(
        issue_id=row["issue_id"],
        repo=row["repo"],
        title=row["title"],
        price=Usdc(int(row["price_base_units"])),
        settled_at=datetime.fromisoformat(row["settled_at"]),
        distance=float(row["distance"]),
    )


def _subscription(row) -> Subscription:  # type: ignore[no-untyped-def]
    return Subscription(
        publisher_id=row["publisher_id"],
        plan=row["plan"],
        status=row["status"],
        period_start=_utc_or_none(row["period_start"]),
        period_end=_utc_or_none(row["period_end"]),
        cancel_at_period_end=row["cancel_at_period_end"],
        pending=PaymentRequest(**row["pending"]) if row["pending"] else None,
        updated_at=_utc(row["updated_at"]),
    )


def _escrow(row: Row) -> EscrowCommitment:
    return EscrowCommitment(
        issue_id=row["issue_id"],
        contract=row["contract"],
        chain=row["chain"],
        tx_hash=row["tx_hash"],
        amount=money(Usdc(row["amount_base_units"])),
        deadline=_utc(row["deadline"]),
        released=row["released"],
        refunded=row["refunded"],
    )


def _claim(row: Row) -> Claim:
    return Claim(
        contributor_id=row["contributor_id"],
        issued_at=_utc(row["issued_at"]),
        expires_at=_utc(row["expires_at"]),
        active=row["active"],
    )


def _submission(row: Row) -> Submission:
    return Submission(
        pr_number=row["pr_number"],
        head_sha=row["head_sha"],
        checks_passed=row["checks_passed"],
        files_changed=row["files_changed"],
        additions=row["additions"],
        deletions=row["deletions"],
    )


def _review(row: Row) -> Review:
    cost = row["cost_base_units"]
    return Review(
        verdict=row["verdict"],
        findings=list(row["findings"]),
        decided_at=_utc(row["decided_at"]),
        head_sha=row["head_sha"],
        reviewer=row["reviewer"],
        seconds=row["seconds"],
        cost_usdc=f"{Usdc(cost).decimal:.6f}" if cost is not None else None,
    )


def _decision(row: Row) -> Decision:
    return Decision(
        id=row["id"],
        issue_id=row["issue_id"],
        actor=row["actor"],
        action=row["action"],
        rule=row["rule"],
        outcome=row["outcome"],
        cost_usdc=row["cost_usdc"],
        created_at=_utc(row["created_at"]),
    )


def _screening(row: Row) -> Screening:
    return Screening(
        party_kind=PartyKind(row["party_kind"]),
        party_id=row["party_id"],
        wallet_address=row["wallet_address"],
        outcome=ScreeningOutcome(row["outcome"]),
        provider=row["provider"],
        reason=ScreeningReason(row["reason"]),
        checked_at=_utc(row["checked_at"]),
        list_name=row["list_name"],
    )


def _money_event(row: Row) -> MoneyEvent:
    return MoneyEvent(
        issue_id=row["issue_id"],
        kind=MoneyEventKind(row["kind"]),
        amount=Usdc(row["amount_base_units"]),
        counterparty_id=row["counterparty_id"],
        tx_hash=row["tx_hash"],
        occurred_at=_utc(row["occurred_at"]),
    )

"""SQL repository: Postgres in a deployment, a SQLite file for durable local runs.

An issue is saved whole inside one transaction, guarded by a version column: the
update only lands when the stored version is the one this copy was loaded at. Child
rows follow the schema's rules. Proposals, submissions, verdicts and decisions are
only ever inserted, the commitment mirror and the claim are updated in place, and an
issue's current proposal, claim, submission and verdict are its newest rows.

The simulation's reset swaps every row for the seed inside one transaction too, so a
reader sees the old data or the seed and never the empty tables between. The version
column carries on climbing across it rather than starting again at 1, so a copy loaded
before the reset is still refused after it.

The schema is brought to the latest migration before first use, so a fresh database
needs no separate setup step and a deployment cannot run against an old schema.
"""

from __future__ import annotations

import dataclasses
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
from misthos.domain.issue import FundingTerms, IssueState
from misthos.domain.ledger import MoneyEvent, MoneyEventKind
from misthos.domain.money import Usdc, format_usdc
from misthos.domain.pricing import PriceProposal
from misthos.models import tables as t
from misthos.models.records import IssueRecord
from misthos.repositories.base import (
    AccountConflict,
    AppendOnlyViolation,
    PaymentAlreadyUsed,
    Seed,
    SsoDomainTaken,
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
    RepoConnection,
    Review,
    SsoConnection,
    Submission,
    Subscription,
    SubscriptionPayment,
    Wallet,
    money,
)

Row = Mapping[str, Any]

# The counter that remembers the highest version any issue reached before the last
# reset. A reset keeps it while every other counter starts again, and an issue saved
# for the first time starts above it.
_VERSION_FLOOR = "issue_version_floor"

# What a reset empties, children first. The simulated chain's books share the
# database but belong to the chain gateway, which resets them itself, before a seed
# is built against them.
_RESET_TABLES = [
    table
    for table in reversed(t.metadata.sorted_tables)
    if table not in (t.simulated_escrow, t.simulated_escrow_ceilings)
]
# The order a reset locks them in under Postgres. Every save takes its issue row
# before any other, so the reset takes issues first too and never holds a table a
# save in flight still needs; publishers and contributors, which other rows point
# at, come last, as they do in every transaction that touches them.
_RESET_LOCK_ORDER = [t.issues, *(table for table in _RESET_TABLES if table is not t.issues)]


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

    def reset(self, seed: Seed | None = None) -> None:
        self.migrate()
        with self.engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                # Writers wait for the reset rather than interleave with it, which
                # deadlocked a save holding an issue the reset was about to delete.
                # Readers are not held up, and see the old rows until the commit.
                quote = conn.dialect.identifier_preparer.quote
                names = ", ".join(quote(table.name) for table in _RESET_LOCK_ORDER)
                conn.execute(text(f"LOCK TABLE {names} IN EXCLUSIVE MODE"))
            floor = _version_floor(conn)
            for table in _RESET_TABLES:
                if table is t.issues:
                    # The versions of the rows actually deleted, so one an action saved
                    # a moment before the reset is counted too.
                    gone = conn.execute(delete(table).returning(table.c.version)).scalars()
                    floor = max([floor, *gone])
                elif table is t.counters:
                    conn.execute(delete(table).where(table.c.name != _VERSION_FLOOR))
                else:
                    conn.execute(delete(table))
            _upsert(conn, t.counters, {"name": _VERSION_FLOOR}, {"value": floor})
            if seed is not None:
                _write_seed(conn, seed, floor)

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
        with self.engine.begin() as conn:
            _upsert(conn, t.publishers, {"id": publisher.id}, _publisher_values(publisher))

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
        with self.engine.begin() as conn:
            _upsert(conn, t.contributors, {"id": contributor.id}, _contributor_values(contributor))

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

    def get_account(self, party_id: str) -> Account | None:
        return self._account_where(t.accounts.c.party_id == party_id)

    def get_account_by_address(self, address: str) -> Account | None:
        # Stored lowercase, as the session names it.
        return self._account_where(t.accounts.c.address == address.lower())

    def get_account_by_github_id(self, github_id: int) -> Account | None:
        return self._account_where(t.accounts.c.github_id == github_id)

    def get_account_by_github_login(self, login: str) -> Account | None:
        return self._account_where(func.lower(t.accounts.c.github_login) == login.lower())

    def _account_where(self, condition: Any) -> Account | None:
        self.migrate()
        with self.engine.connect() as conn:
            row = conn.execute(select(t.accounts).where(condition)).mappings().first()
        return _account(row) if row else None

    def save_account(self, account: Account) -> None:
        self.migrate()
        values = {
            "role": account.role,
            "address": account.address.lower() if account.address else None,
            "github_id": account.github_id,
            "github_login": account.github_login,
            "created_at": account.created_at,
        }
        try:
            with self.engine.begin() as conn:
                # GitHub treats a login case-insensitively and the unique constraint does
                # not, so the case is checked here, as the memory repository does.
                if account.github_login and conn.execute(
                    select(t.accounts.c.party_id).where(
                        func.lower(t.accounts.c.github_login) == account.github_login.lower(),
                        t.accounts.c.party_id != account.party_id,
                    )
                ).first():
                    raise AccountConflict(f"{account.github_login} is linked to another account")
                _upsert(conn, t.accounts, {"party_id": account.party_id}, values)
        except IntegrityError as exc:
            # The store checks each of these first and says which; this is the race
            # between two requests, which the unique constraints settle.
            raise AccountConflict(
                "that wallet or GitHub account belongs to another account"
            ) from exc

    # ---------------------------------------------------------- GitHub connections

    def get_connection(self, repo: str) -> RepoConnection | None:
        self.migrate()
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(t.repo_connections).where(t.repo_connections.c.repo == repo.lower())
                )
                .mappings()
                .first()
            )
        return _connection(row) if row else None

    def save_connection(self, connection: RepoConnection) -> None:
        self.migrate()
        values = {
            "installation_id": connection.installation_id,
            "installed_by": connection.installed_by,
            "publisher_id": connection.publisher_id,
            "connected_at": connection.connected_at,
        }
        with self.engine.begin() as conn:
            _upsert(conn, t.repo_connections, {"repo": connection.repo.lower()}, values)

    def delete_connections(self, repos: list[str]) -> None:
        self.migrate()
        with self.engine.begin() as conn:
            conn.execute(
                delete(t.repo_connections).where(
                    t.repo_connections.c.repo.in_([r.lower() for r in repos])
                )
            )

    def list_connections(self) -> list[RepoConnection]:
        self.migrate()
        with self.engine.connect() as conn:
            rows = conn.execute(select(t.repo_connections)).mappings().all()
        return [_connection(r) for r in rows]

    # --------------------------------------------------------- single sign-on

    def get_sso(self, publisher_id: str) -> SsoConnection | None:
        self.migrate()
        with self.engine.connect() as conn:
            return _sso(conn, t.sso_connections.c.publisher_id == publisher_id)

    def get_sso_by_domain(self, domain: str) -> SsoConnection | None:
        self.migrate()
        owner = select(t.sso_domains.c.publisher_id).where(
            t.sso_domains.c.domain == domain.lower()
        )
        with self.engine.connect() as conn:
            return _sso(conn, t.sso_connections.c.publisher_id.in_(owner))

    def save_sso(self, connection: SsoConnection) -> None:
        self.migrate()
        domains = [d.lower() for d in connection.domains]
        values = {
            "issuer": connection.issuer,
            "client_id": connection.client_id,
            "client_secret_ref": connection.client_secret_ref,
            "required": connection.required,
            "configured_at": connection.configured_at,
        }
        try:
            with self.engine.begin() as conn:
                taken = conn.execute(
                    select(t.sso_domains.c.domain).where(
                        t.sso_domains.c.domain.in_(domains),
                        t.sso_domains.c.publisher_id != connection.publisher_id,
                    )
                ).scalars().first()
                if taken is not None:
                    raise SsoDomainTaken(
                        f"{taken} signs in to another organisation's identity provider"
                    )
                _upsert(conn, t.sso_connections, {"publisher_id": connection.publisher_id}, values)
                conn.execute(
                    delete(t.sso_domains).where(
                        t.sso_domains.c.publisher_id == connection.publisher_id
                    )
                )
                conn.execute(
                    insert(t.sso_domains),
                    [{"domain": d, "publisher_id": connection.publisher_id} for d in domains],
                )
        except IntegrityError as exc:
            # Two organisations claiming one domain at once: the primary key settles it.
            raise SsoDomainTaken(
                "one of those domains signs in to another organisation's identity provider"
            ) from exc

    def delete_sso(self, publisher_id: str) -> None:
        self.migrate()
        with self.engine.begin() as conn:
            conn.execute(delete(t.sso_domains).where(t.sso_domains.c.publisher_id == publisher_id))
            conn.execute(
                delete(t.sso_connections).where(t.sso_connections.c.publisher_id == publisher_id)
            )

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
            floor = _version_floor(conn) if any(r.version == 0 for r in records) else 0
            versions = [_save(conn, rec, floor) for rec in records]
        # Only once the transaction has committed do the copies become current.
        for rec, version in zip(records, versions, strict=True):
            rec.version = version

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


def _version_floor(conn: Connection) -> int:
    found = conn.execute(
        select(t.counters.c.value).where(t.counters.c.name == _VERSION_FLOOR)
    ).scalar()
    return int(found or 0)


def _write_seed(conn: Connection, seed: Seed, floor: int) -> None:
    for publisher in seed.publishers:
        _upsert(conn, t.publishers, {"id": publisher.id}, _publisher_values(publisher))
    for contributor in seed.contributors:
        _upsert(conn, t.contributors, {"id": contributor.id}, _contributor_values(contributor))
    for rec in seed.issues:
        # New rows here, numbered from the floor like any issue saved for the first time.
        _save(conn, dataclasses.replace(rec, version=0), floor)
    for name, value in seed.counters.items():
        if name != _VERSION_FLOOR:
            _upsert(conn, t.counters, {"name": name}, {"value": value})


def _publisher_values(publisher: Publisher) -> dict[str, Any]:
    return {
        "name": publisher.name,
        "kind": publisher.kind,
        "tier": publisher.tier,
        "wallet_address": publisher.wallet.address if publisher.wallet else None,
        "chain": publisher.wallet.chain if publisher.wallet else None,
        "circle_wallet_address": (
            publisher.circle_wallet.address if publisher.circle_wallet else None
        ),
        "circle_wallet_chain": publisher.circle_wallet.chain if publisher.circle_wallet else None,
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


def _contributor_values(contributor: Contributor) -> dict[str, Any]:
    return {
        "handle": contributor.handle,
        "wallet_address": contributor.wallet.address if contributor.wallet else None,
        "chain": contributor.wallet.chain if contributor.wallet else None,
        "reputation": contributor.reputation,
        "settled_issues": contributor.settled_issues,
        "earned_base_units": _parse_usdc(contributor.earned_usdc).base_units,
        "identity_status": contributor.identity_status,
        "identity_reference": contributor.identity_reference,
        "identity_verified_at": contributor.identity_verified_at,
    }


def _save(conn: Connection, rec: IssueRecord, floor: int) -> int:
    """Write one issue and return the version it now has."""
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
        "platform_fee_base_units": rec.platform_fee.base_units if rec.platform_fee else None,
        "paid_at": rec.paid_at,
        "payout_tx_hash": rec.payout_tx_hash,
        "accepted_by": rec.accepted_by,
        "payout_hold": rec.payout_hold,
        "payout_checked_at": rec.payout_checked_at,
        "criteria_approved_at": rec.criteria_approved_at,
        "relisted_from": rec.relisted_from,
        **_funding_values(rec.funding),
    }
    if rec.version == 0:
        exists = conn.execute(select(t.issues.c.id).where(t.issues.c.id == rec.id)).first()
        if exists is not None:
            raise StaleIssue(rec.id)
        version = floor + 1
        conn.execute(insert(t.issues).values(id=rec.id, version=version, **values))
    else:
        version = rec.version + 1
        written = conn.execute(
            update(t.issues)
            .where(t.issues.c.id == rec.id, t.issues.c.version == rec.version)
            .values(version=version, **values)
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
    return version


def _funding_values(terms: FundingTerms | None) -> dict[str, Any]:
    return {
        "funding_amount_base_units": terms.amount.base_units if terms else None,
        "funding_fee_bps": terms.fee_bps if terms else None,
        "funding_wallet": terms.wallet if terms else None,
        "funding_deadline": terms.deadline if terms else None,
        "funding_approved_at": terms.approved_at if terms else None,
    }


def _funding(row: Row) -> FundingTerms | None:
    if row["funding_amount_base_units"] is None:
        return None
    return FundingTerms(
        amount=Usdc(row["funding_amount_base_units"]),
        fee_bps=row["funding_fee_bps"],
        wallet=row["funding_wallet"],
        deadline=_utc(row["funding_deadline"]),
        approved_at=_utc(row["funding_approved_at"]),
    )


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
            "money": e.money,
            "tx_hash": e.tx_hash,
            "amount_base_units": int(e.amount["base_units"]),
            "deadline": e.deadline,
            "fee_bps": e.fee_bps,
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
            platform_fee=(
                Usdc(row["platform_fee_base_units"])
                if row["platform_fee_base_units"] is not None
                else None
            ),
            paid_at=_utc_or_none(row["paid_at"]),
            payout_tx_hash=row["payout_tx_hash"],
            accepted_by=row["accepted_by"],
            payout_hold=row["payout_hold"],
            payout_checked_at=_utc_or_none(row["payout_checked_at"]),
            criteria_approved_at=_utc_or_none(row["criteria_approved_at"]),
            relisted_from=row["relisted_from"],
            funding=_funding(row),
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
        wallet=_wallet(row["wallet_address"], row["chain"]),
        circle_wallet=(
            Wallet(address=row["circle_wallet_address"], chain=row["circle_wallet_chain"])
            if row["circle_wallet_address"]
            else None
        ),
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


def _account(row: Row) -> Account:
    return Account(
        address=row["address"],
        role=row["role"],
        party_id=row["party_id"],
        github_id=row["github_id"],
        github_login=row["github_login"],
        created_at=_utc(row["created_at"]),
    )


def _wallet(address: str | None, chain: str | None) -> Wallet | None:
    return Wallet(address=address, chain=chain or "") if address else None


def _contributor(row: Row) -> Contributor:
    return Contributor(
        id=row["id"],
        handle=row["handle"],
        wallet=_wallet(row["wallet_address"], row["chain"]),
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


def _connection(row) -> RepoConnection:  # type: ignore[no-untyped-def]
    return RepoConnection(
        repo=row["repo"],
        installation_id=int(row["installation_id"]),
        installed_by=row["installed_by"],
        publisher_id=row["publisher_id"],
        connected_at=_utc(row["connected_at"]),
    )


def _sso(conn: Connection, where: Any) -> SsoConnection | None:
    row = conn.execute(select(t.sso_connections).where(where)).mappings().first()
    if row is None:
        return None
    domains = conn.execute(
        select(t.sso_domains.c.domain).where(t.sso_domains.c.publisher_id == row["publisher_id"])
    ).scalars()
    return SsoConnection(
        publisher_id=row["publisher_id"],
        issuer=row["issuer"],
        client_id=row["client_id"],
        client_secret_ref=row["client_secret_ref"],
        # Sorted here, not by the database: Postgres collates by locale and puts
        # `acme.example` before `acme-labs.example`, which the memory store does not.
        domains=sorted(domains),
        required=row["required"],
        configured_at=_utc(row["configured_at"]),
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
        money=row["money"],
        tx_hash=row["tx_hash"],
        amount=money(Usdc(row["amount_base_units"])),
        deadline=_utc(row["deadline"]),
        fee_bps=row["fee_bps"],
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

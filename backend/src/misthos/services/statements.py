"""Per-contributor annual statements.

A platform that pays small amounts to many people in many jurisdictions creates
reporting work for each of them, and the businesses that pay through it will not take
that work on. The statement is the record a contributor, or their accountant, files
from: every payout in a calendar year with its date, issue, counterparty, amount and
settlement reference, and a total.

It is built from the money ledger's releases, each carrying the transfer the chain
returned, so it shows what was paid rather than what was promised. Statements are
personal: they carry settlement references that would link a wallet to a GitHub
handle, so they are served only to the contributor (see docs/PRIVACY.md), and from
the command line for an operator:

    python -m misthos.services.statements CON-1 2026 --csv
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from datetime import UTC, datetime

from misthos.config import settings
from misthos.domain.issue import IssueState
from misthos.domain.ledger import MoneyEventKind
from misthos.domain.money import Usdc
from misthos.schemas import AnnualStatement, StatementLine, money
from misthos.store import Store

CSV_COLUMNS = [
    "paid_at_utc",
    "issue_id",
    "repo",
    "issue_number",
    "issue_title",
    "counterparty_id",
    "counterparty_name",
    "amount",
    "currency",
    "chain",
    "tx_hash",
]


def annual_statement(store: Store, contributor_id: str, year: int) -> AnnualStatement | None:
    """Every payout to one contributor in one UTC calendar year, or None if unknown."""
    contributor = store.get_contributor(contributor_id)
    if contributor is None:
        return None
    publishers = {p.id: p.name for p in store.list_publishers()}
    releases = sorted(
        (
            (event, rec)
            for rec in store.list_issues({IssueState.PAID})
            for event in rec.money_events
            if event.kind is MoneyEventKind.RELEASED
            and event.counterparty_id == contributor_id
            and event.occurred_at.astimezone(UTC).year == year
        ),
        key=lambda pair: pair[0].occurred_at,
    )
    lines = [
        StatementLine(
            paid_at=event.occurred_at,
            issue_id=rec.id,
            repo=rec.repo,
            issue_number=rec.number,
            issue_title=rec.title,
            counterparty_id=rec.publisher_id,
            counterparty_name=publishers.get(rec.publisher_id, rec.publisher_id),
            amount=money(event.amount),
            chain=rec.escrow.chain if rec.escrow else settings.chain,
            tx_hash=event.tx_hash,
        )
        for event, rec in releases
    ]
    total = Usdc(sum(event.amount.base_units for event, _ in releases))
    return AnnualStatement(
        contributor_id=contributor.id,
        handle=contributor.handle,
        year=year,
        identity_status=contributor.identity_status,
        lines=lines,
        total=money(total),
        payouts=len(lines),
        generated_at=datetime.now(UTC),
        simulated=settings.simulated,
    )


def to_csv(statement: AnnualStatement) -> str:
    """One row per payout, then a total row, in the columns an accountant expects."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    if statement.simulated:
        # A simulated statement must never be mistaken for a tax record.
        writer.writerow(["# SIMULATED: no chain was contacted; not a tax record"])
    writer.writerow(CSV_COLUMNS)
    for line in statement.lines:
        writer.writerow(
            [
                line.paid_at.astimezone(UTC).isoformat(),
                line.issue_id,
                line.repo,
                line.issue_number,
                line.issue_title,
                line.counterparty_id,
                line.counterparty_name,
                line.amount["usdc"],
                line.currency,
                line.chain,
                line.tx_hash or "",
            ]
        )
    writer.writerow(["TOTAL", "", "", "", "", "", "", statement.total["usdc"], "USDC", "", ""])
    return out.getvalue()


def main(argv: list[str] | None = None) -> int:
    from misthos.store import store

    parser = argparse.ArgumentParser(description="Export one contributor's annual statement.")
    parser.add_argument("contributor_id")
    parser.add_argument("year", type=int)
    parser.add_argument("--csv", action="store_true", help="CSV instead of JSON")
    args = parser.parse_args(argv)

    statement = annual_statement(store, args.contributor_id, args.year)
    if statement is None:
        print(f"no contributor {args.contributor_id}", file=sys.stderr)
        return 1
    sys.stdout.write(to_csv(statement) if args.csv else statement.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

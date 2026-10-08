"""The audit export: one organisation's decision record and money events (#52).

The decision record is what makes delegated authority defensible, and rebuilding it
in a spreadsheet is the work Misthos sells against. So the export is the record
itself: every decision in order with its actor, rule, outcome and cost, and every
money event with its amount, counterparty and the transfer that carried it,
unedited, as JSON or as one CSV an auditor can sort and filter. The one thing left
out is the transfer reference of each release, which would link a contributor's
wallet to their handle; it is on the contributor's own statement (docs/PRIVACY.md).
It is the organisation's own record, so it is served to them alone (in the
simulation until sign-in exists) and exported by an operator:

    python -m misthos.services.audit PUB-1 --csv
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import UTC, datetime

from misthos.config import settings
from misthos.domain.ledger import MoneyEventKind
from misthos.schemas import AuditExport, AuditIssue, AuditMoneyEvent, money
from misthos.services import spreadsheet
from misthos.store import Store

CSV_COLUMNS = [
    "record",
    "issue_id",
    "repo",
    "issue_number",
    "at_utc",
    "actor_or_kind",
    "action",
    "rule",
    "outcome",
    "amount_usdc",
    "counterparty_id",
    "tx_hash",
    "decision_id",
]

# A last row rather than a first one, so the header stays the header. Written into the
# `record` column, which is where a reader looks for what kind of row it is.
SIMULATED_NOTE = ["# SIMULATED: no chain was contacted; not an audit record"]


def export(store: Store, publisher_id: str) -> AuditExport | None:
    publisher = store.get_publisher(publisher_id)
    if publisher is None:
        return None
    issues = sorted(
        (r for r in store.list_issues() if r.publisher_id == publisher_id),
        key=lambda r: (r.created_at, r.id),
    )
    return AuditExport(
        publisher_id=publisher.id,
        name=publisher.name,
        generated_at=datetime.now(UTC),
        simulated=settings.simulated,
        issues=[
            AuditIssue(
                id=r.id,
                repo=r.repo,
                number=r.number,
                title=r.title,
                state=r.state.value,
                labels=r.labels,
                compliance_driven=r.compliance_driven,
                acceptance_criteria=r.acceptance_criteria,
                created_at=r.created_at,
                price=money(r.proposal.recommended) if r.proposal else None,
                decisions=r.decisions,
                money_events=[
                    AuditMoneyEvent(
                        occurred_at=e.occurred_at,
                        kind=e.kind.value,
                        amount=money(e.amount),
                        counterparty_id=e.counterparty_id,
                        tx_hash=None if e.kind is MoneyEventKind.RELEASED else e.tx_hash,
                    )
                    for e in r.money_events
                ],
            )
            for r in issues
        ],
    )


def to_csv(audit: AuditExport) -> str:
    """One row per decision and per money event, in time order within each issue.

    The header is the first line, so `csv.DictReader` and a spreadsheet agree on what
    the columns are. The simulation caveat goes in the last row instead: a reader that
    takes the first line for the header would otherwise misalign every column, and a
    comment before it is what the round trip cannot survive.

    Repositories, reasons and logins are other people's words, so every cell is
    written as text a spreadsheet will not run (services/spreadsheet.py).
    """
    out = io.StringIO()
    writer = spreadsheet.Writer(out)
    writer.writerow(CSV_COLUMNS)
    for issue in audit.issues:
        rows: list[tuple[datetime, list[object]]] = []
        for d in issue.decisions:
            rows.append(
                (
                    d.created_at,
                    ["decision", issue.id, issue.repo, issue.number,
                     d.created_at.astimezone(UTC).isoformat(), d.actor, d.action, d.rule,
                     d.outcome, d.cost_usdc or "", "", "", d.id],
                )
            )  # fmt: skip
        for e in issue.money_events:
            rows.append(
                (
                    e.occurred_at,
                    ["money", issue.id, issue.repo, issue.number,
                     e.occurred_at.astimezone(UTC).isoformat(), e.kind, "", "", "",
                     e.amount["usdc"], e.counterparty_id, e.tx_hash or "", ""],
                )
            )  # fmt: skip
        for _, row in sorted(rows, key=lambda pair: pair[0]):
            writer.writerow(row)
    if audit.simulated:
        # A data row, not a comment, so every column stays where the header put it.
        writer.writerow(
            SIMULATED_NOTE + [""] * (len(CSV_COLUMNS) - len(SIMULATED_NOTE))
        )
    return out.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export one publisher's audit record")
    parser.add_argument("publisher_id")
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args(argv)

    from misthos.store import store

    found = export(store, args.publisher_id)
    if found is None:
        print(f"no publisher {args.publisher_id}", file=sys.stderr)
        return 1
    print(to_csv(found) if args.csv else found.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

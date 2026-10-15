"""Answer support requests under the Enterprise commitment (#53), from the operator's side.

    python -m misthos.services.support list [--overdue]
    python -m misthos.services.support answer SUP-1 --by "Ana at Misthos" --message "..."

The organisation opens a request in the web app; we answer here, as an operator records
an Enterprise contract (`services/contracts.py`). Only the first response is recorded,
because it is what the commitment measures; the conversation continues wherever the
first response takes it.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="every request, newest first")
    listing.add_argument("--overdue", action="store_true", help="only the late ones")
    answer = commands.add_parser("answer", help="record the first response to a request")
    answer.add_argument("request_id")
    answer.add_argument("--by", required=True, help="who is answering, as the customer sees it")
    answer.add_argument("--message", required=True)
    args = parser.parse_args(argv)

    from misthos.store import AlreadyAnswered, store

    if args.command == "list":
        for r in store.support_requests():
            if args.overdue and not r.overdue:
                continue
            answered = r.first_response_at
            state = f"answered {answered:%Y-%m-%d %H:%M}" if answered else "open"
            late = " LATE" if r.overdue else ""
            print(
                f"{r.id} {r.publisher_id} {r.severity:<6} due {r.respond_by:%Y-%m-%d %H:%M} UTC "
                f"{state}{late}: {r.subject}"
            )
        return 0
    try:
        answered = store.answer_support(args.request_id, responder=args.by, message=args.message)
    except KeyError:
        print(f"no support request {args.request_id}", file=sys.stderr)
        return 1
    except AlreadyAnswered as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{answered.id} answered{' late' if answered.overdue else ' in time'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

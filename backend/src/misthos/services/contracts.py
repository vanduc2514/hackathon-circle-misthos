"""Put an organisation on a plan agreed with us, such as Enterprise (#53).

    python -m misthos.services.contracts PUB-1 enterprise --until 2027-10-31

Enterprise is priced "from" a figure and agreed in a contract, so it is never bought
from the web app; an operator records it here once it is signed. It ends on the date
given, and the sweeper treats that like any other period end.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from misthos.domain import plans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("publisher_id")
    parser.add_argument("plan", choices=sorted(plans.PLANS))
    parser.add_argument("--until", required=True, help="the contract's end, YYYY-MM-DD")
    args = parser.parse_args(argv)

    from misthos.store import store

    until = datetime.fromisoformat(args.until).replace(tzinfo=UTC)
    out = store.set_contract_plan(args.publisher_id, args.plan, until)
    print(f"{out.publisher_id} is on {out.plan} until {out.period_end:%Y-%m-%d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

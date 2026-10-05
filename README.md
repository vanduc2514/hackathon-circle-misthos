# Misthos

A marketplace where a company or an open-source maintainer puts a **fixed price** on
a GitHub issue and pays whoever fixes it, settled in USDC on Arc.

An agent prices the issue from its complexity and the publisher's own budget, another
agent reviews the submitted pull request against the acceptance criteria, and a human
keeps the two decisions that carry consequences: approving the price, and accepting
the work. Payment is released by a contract when the pull request merges.

## Status

Scaffold with a working simulation. The lifecycle, the pricing engine, the escrow
contract and the decision log are real code. The GitHub calls and the money are fake:
no chain is contacted.

| Piece | State |
| --- | --- |
| Domain: lifecycle, money units, pricing engine | Real, 32 unit tests |
| API: issues, proposals, metrics, decisions, webhooks | Real, 16 integration tests |
| Web app: dashboard, issue list, issue detail with lifecycle controls | Real, running |
| Escrow contract | Real Solidity, 18 tests passing on Foundry |
| Edge service: x402 gate and Circle CLI bridge | Stubbed, typechecks |
| GitHub App, real chain settlement, identity verification | Not started |

## Layout

```
backend/      Python: FastAPI API, domain logic, agents, workers
frontend/     Vite + React SPA, typed client generated from the API schema
edge/         Node: x402 payment gate and Circle CLI wallet operations
contracts/    Foundry: MisthosEscrow on Arc
docs/         Business documentation and the architecture
```

Four code folders, one per runtime boundary. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for why the boundary sits there, and
[docs/misthos/README.md](docs/misthos/README.md) for the product thinking.

## Running it

Requires [mise](https://mise.jdx.dev/), which pins Node 22, Python 3.11, uv and
Foundry. Install the toolchain once, then the dependencies:

```bash
mise install
mise run setup
```

`mise run setup` also clones `forge-std` into `contracts/lib/`, which is gitignored
rather than vendored. If you only want the contract tests:

```bash
mkdir -p contracts/lib && git clone --depth 1 \
  https://github.com/foundry-rs/forge-std.git contracts/lib/forge-std
```

Start the API and the web app together:

```bash
mise run dev
```

Or separately, if you want two terminals:

```bash
mise run dev:api     # FastAPI on http://127.0.0.1:8000
mise run dev:web     # Vite on http://localhost:5173
```

Open <http://localhost:5173>. The API reference is at
<http://127.0.0.1:8000/docs>, and the web app proxies `/docs` so it is reachable
from the same origin.

Both `localhost:5173` and `127.0.0.1:5173` work. Vite is configured with `host: true`
because without it the dev server binds IPv6 only and `127.0.0.1` refuses the
connection.

## What to look at

1. **Overview** shows the north star, the metrics behind it, and the decision log.
2. **Issues** lists all eight seeded issues with filters.
3. Open any issue to see the pricing engine's reasoning: the band, the six
   complexity signals, the effort estimate and the justification.

To drive the lifecycle, open an issue that is awaiting approval and press
**Approve price and commit funds**. The escrow panel appears with the commitment, the
state moves, and the decision log gains an entry. **Run to settlement** finishes the
happy path, including the review fee.

`mise run reset` puts the seeded data back.

## Tests

```bash
mise run test            # everything
mise run test:backend    # pytest, domain and API
mise run test:web        # vitest, formatting and time helpers
mise run test:contracts  # forge test, escrow release and refund
mise run lint            # ruff, tsc for both TypeScript packages
```

## The simulated data

Eight issues in different lifecycle states, five publishers, six contributors and
three reviewers. Prices come from the real pricing engine, so the numbers move when
the signals change. Two issues are compliance-driven, which is where the EU Cyber
Resilience Act argument lands.

Reset at any time:

```bash
mise run reset
# or
curl -X POST http://127.0.0.1:8000/api/v1/demo/reset
```

## Stack

| Layer | Choice | Why |
| --- | --- | --- |
| API | FastAPI, Pydantic v2 | Async, and it generates the OpenAPI schema the frontend types come from |
| Domain | Pure Python, no IO | The lifecycle and the money units are where a bug costs real funds, so they are testable without a database |
| Web | Vite 6, React 19, TypeScript | SPA against a separate API, so no SSR and one origin via a proxy |
| Data fetching | TanStack Query | Retries, caching and cache invalidation after a lifecycle action |
| API client | `openapi-typescript` + `openapi-fetch` | Types are generated from the live schema, so drift is a compile error |
| Contracts | Solidity 0.8.24, Foundry | Osaka EVM baseline, which is what Arc targets |
| Edge | Express, TypeScript | Circle's x402 seller middleware is Node only |

## Two things worth knowing before editing

**Arc's USDC has two views of one balance.** Native gas accounting is 18 decimals;
the ERC-20 at `0x3600000000000000000000000000000000000000` is 6 decimals. They are the
same money. `backend/src/misthos/domain/money.py` makes them separate types so they
cannot be mixed by accident, and everything except raw gas math uses the 6-decimal
view.

**The review agent runs the repository's own checks.** It does not execute contributor
code. A pull request is untrusted input, so the sandbox is GitHub Actions and we read
the results. See the architecture document.

## Documentation

| Document | What it covers |
| --- | --- |
| [docs/misthos/](docs/misthos/README.md) | Business case: problem, market research, personas, pricing, economics, risk, metrics, go to market |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, settlement paths, the escrow contract, trust boundaries, open decisions |
| [docs/misthos/11-hackathon-alignment.md](docs/misthos/11-hackathon-alignment.md) | How this maps to the Tameion brief and its judging rubric |

## Licence

Not yet chosen. The escrow contract and the pricing engine are the parts most likely to
be reused, so a permissive licence is the likely direction.

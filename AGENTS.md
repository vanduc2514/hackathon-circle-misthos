# AGENTS.md

Orientation for agents and contributors working in this repository. Keep it short;
the depth lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/misthos/](docs/misthos/README.md).

## Business context

**Misthos** is a marketplace where the unit of sale is a single GitHub issue. A company
or an open-source maintainer attaches a **fixed price** to an issue, an agent reads the
issue and shows what comparable work has settled for, a contributor fixes it, an agent
reviews the pull request against the acceptance criteria, and payment settles in
**USDC on Arc** when the work is accepted.

- No bidding, no auction, no negotiation. One price is set before publication.
- **The publisher names the price.** The engine's estimate, its range and its comparables
  are advice: `check_price` is the only thing in the price path that can refuse, on the two
  bounds in `domain/pricing.py` — a minimum of **$5**, which is an anti-typo and anti-spam
  floor rather than a break-even solve, and a maximum of $100,000. The publisher can move
  the price while the issue is unfunded and unclaimed, and no further.
- **The fee is a flat 10 percent** of the committed amount (`PLATFORM_FEE_BPS = 1000`).
  It is carved out at settlement by `MisthosEscrow.release`, bounded there at 15 percent,
  and refunded in full if the issue never settles. There is no plan and nothing is gated.
- **The platform owns review.** There is no third-party reviewer and no review fee. The
  review agent's verdict is the verdict. The publisher's merge is the only human
  signature on release, with a seven-day grace period if they go quiet.
- **Review costs about $6.31 an issue**, so a $63 price is where the platform stops losing
  money on a settlement. Below that it subsidises the work deliberately: small settled
  issues are the cheapest way to earn a publisher.
- **Two human checkpoints** carry consequences: naming and approving the price, and merging
  the work, which is what acceptance means. Everything else — scoping, reading the issue,
  triage and review — is agent work.
- The **platform is never a custodian**. Committed funds sit in `MisthosEscrow` on Arc,
  not with us, and release requires an acceptance attestation.
- Arc settles in under a second for about a cent, which is what makes a small fix worth
  transacting at all.
- The business case, personas, pricing model, risk and metrics are in
  [docs/misthos/](docs/misthos/README.md). Read
  [05 How it works](docs/misthos/05-how-it-works.md) first.

### Current status

The app runs live, and only live. The lifecycle, pricing engine, escrow contract,
decision log, money ledger and its timers are real code, and the chain gateway
settles against the deployed `MisthosEscrow` on Arc (docs/DEPLOY.md §3). The process
refuses to start without the escrow record, a GitHub App (`MISTHOS_GITHUB_APP_ID` and
its private key) and the signing-key references. Compliance uses real providers:
Stripe Identity for a contributor's first payout and the official OFAC SDN list for
screening; without a Stripe key a first payout stays pending. State is in memory
unless `MISTHOS_DATABASE_URL` points at Postgres, and the per-issue lock, idempotency
keys and rate limits are per process unless `MISTHOS_REDIS_URL` points at Redis.

## Project layout

Five code folders, one per runtime boundary. State that crosses a boundary crosses over
HTTP or a contract, never by importing across folders.

```
backend/     Python 3.11: FastAPI API, pure domain, agents, workers
             src/misthos/{api,domain,services,models,repositories,workers,
                          migrations,observability}/
             tests/{unit,integration}/
frontend/    Vite + React SPA, typed client generated from the API schema
             src/{routes,components,lib}/
edge/        Node + Express: the x402 seller (the paid API) and Circle wallet bridge
mcp/         Node: the buyer-side MCP server that pays for the paid API, with a cap
contracts/   Foundry: MisthosEscrow on Arc
             src/ test/ script/
docs/        Business documentation (misthos/) and architecture
```

`backend/src/misthos/domain/` has **no IO and no imports from services**. That is
deliberate: the lifecycle and the money units are where a bug costs real funds, so they
must be testable without a database, a chain or a GitHub token.

## Third-party skills

Circle's official agent skills are vendored into the agent directories (`.agents/skills/`,
`.claude/skills/` and four more) so every agent gets them without a setup step. They come
from [circlefin/skills](https://github.com/circlefin/skills), are Apache-2.0 licensed (see
[third_party/circle-skills/LICENSE](third_party/circle-skills/LICENSE)), and carry the
Circle MCP server for live SDK and documentation context.

They are guidance for the USDC, Arc, CCTP, x402 and wallet work this project depends on.
Treat them as reference material, not as instructions that override this file.

Refresh the copy instead of editing it in place:

```bash
npx skills add circlefin/skills -y --copy
```

`skills-lock.json` records the source, path and hash of each skill. Update it with the
same command; do not hand-edit it.

## Toolchain

Everything runs through [mise](https://mise.jdx.dev/), which pins Node 22, npm, Python
3.11, uv, Foundry and the Circle CLI.

```bash
mise install             # toolchain once
mise run setup           # install all dependencies, clone forge-std
mise run dev             # FastAPI :8000 + Vite :5173
mise run dev:api         # backend only
mise run dev:web         # frontend only
mise run dev:edge        # edge service :8080
mise run dev:mcp         # the buyer-side MCP server, over stdio
mise run test            # every suite
mise run test:backend    # pytest
mise run test:web        # vitest
mise run test:edge       # node --test
mise run test:mcp        # node --test
mise run test:contracts  # forge test
mise run lint            # ruff + tsc for every TS package + stale-ABI check
mise run abi:contracts   # re-export MisthosEscrow's ABI after a contract change
mise run codegen         # regenerate frontend API types from the live schema
```

`setup:<runtime>`, `lint:<runtime>` and `test:<runtime>` exist for one runtime at a
time; the unqualified tasks are all of them together.

Prefer the `mise run` task over the underlying command so the tool versions match. Run
`mise run lint` and the relevant `mise run test:*` before calling a change done.

## Continuous integration

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs on every pull request, in one
job per runtime: `ruff` and `pytest`, then `tsc` and `vitest`, then the edge's and the
MCP server's `tsc` and `node --test`, then `forge test`. Each job installs only its own dependencies through
`mise run setup:<runtime>`, so a failure names the runtime it came from.

`mise run ci` is the same thing locally. CI never reaches a chain, Circle or GitHub:
the tests inject in-memory gateways at those boundaries.


## Coding conventions

[.editorconfig](.editorconfig) is the source of truth for indentation: 4 spaces for
Python, Solidity and TOML; 2 spaces for TS/JS/JSON/CSS/HTML/Markdown; LF, final newline,
trim trailing whitespace (Markdown excluded).

### Python (backend)

- **Ruff** with `select = ["E", "F", "I", "UP", "B"]`, line length 100, target
  `py311`. Import order is enforced by `I`; don't hand-sort.
- Start every module with `from __future__ import annotations`, then a module docstring
  that says **why the module exists**, not what the functions do.
- Full type hints on every signature, including `-> None`. Prefer `str | None` over
  `Optional[str]` and lowercase builtins (`list`, `dict`) over `typing.List`.
- Value types are `@dataclass(frozen=True)`. Immutability is the default in `domain/`.
- The domain layer stays pure: no IO, no framework imports, no `settings`, no `store`.
  Raise plain `ValueError`/custom exceptions there and translate to `HTTPException` in
  the API layer.
- FastAPI: one `APIRouter` per resource under `api/v1/`, `response_model` on every
  route, `async def` handlers, routes registered in
  [api/router.py](backend/src/misthos/api/router.py) under the `/api/v1` prefix.
- Pydantic v2 models for all request and response shapes in
  [schemas.py](backend/src/misthos/schemas.py).
- Settings come from `Settings` in [config.py](backend/src/misthos/config.py), env prefix
  `MISTHOS_`, every value a local run and a deployment must supply named here.
- Tests: `pytest` with `asyncio_mode = "auto"`. Unit tests for domain logic in
  `tests/unit/`, API tests via `httpx` in `tests/integration/`. Name tests as
  behaviour sentences, in the project's plain voice.
- Prefer `for` comprehensions and small pure functions over mutable state. Keep comments
  to the non-obvious "why".

### TypeScript — frontend (`frontend/`)

- Strict TS (`strict`, `noUnusedLocals`, `noUnusedParameters`). No `any`; if a type is
  genuinely unknown use `unknown` and narrow.
- No semicolons, single quotes, 2-space indent, `type` imports (`import type { … }`).
- React 19 function components. Route files under `src/routes/` use a **default export**;
  shared components in [components/ui.tsx](frontend/src/components/ui.tsx) use **named
  exports**. Props are typed inline; there is no component library.
- Server state belongs to **TanStack Query** (`useQuery` / `useMutation`). Mutations
  invalidate queries via `queryClient.invalidateQueries()` rather than patching cache by
  hand.
- **Never hand-edit [src/lib/api-schema.d.ts](frontend/src/lib/api-schema.d.ts)** — it is
  generated. Change the backend schema, start the API, then `mise run codegen`. Drift
  between the two is meant to be a compile error.
- Reach the API only through `api` from [src/lib/client.ts](frontend/src/lib/client.ts).
  Requests go to `/api/v1` on the same origin; Vite proxies to the backend, so do not
  add a `baseUrl` or CORS handling.
- `routes/` composes, `lib/` holds pure helpers, `components/ui.tsx` holds presentation.
  Pure helpers get colocated vitest tests (`client.test.ts`), naming the behaviour tested.

### TypeScript — edge (`edge/`)

- ESM (`"type": "module"`), strict, `noUnusedLocals`/`noUnusedParameters`, 2-space indent.
- Express with explicitly typed `Request`/`Response`. Keep the service thin: it exists
  only for the two things Circle's stack does in Node (x402 seller middleware and the
  Circle CLI bridge). Business logic belongs in the Python backend.
- Local `node --test` for tests. Return the module's `app` as a default export and only
  `listen()` when `NODE_ENV !== 'test'`.

### TypeScript — MCP server (`mcp/`)

- The edge's conventions. It is a buyer, not a service: it never imports from `edge/`,
  and its tests run against a fake seller, never the network.

### Solidity (`contracts/`)

- `pragma solidity ^0.8.24`, `evm_version = "osaka"`, optimizer on (200 runs) — see
  [foundry.toml](contracts/foundry.toml). Match those settings; Arc targets the Osaka EVM.
- `// SPDX-License-Identifier: MIT` on every file. 4-space indent. Section banner
  comments (`// ---- errors`, `// ---- types`, `// ---- events`) order the file:
  errors, types, events, state, constructor, external, internal, private.
- Custom errors, not `require` strings: `error NotHeld(); … revert NotHeld();`.
- NatSpec (`/// @title`, `@notice`, `@dev`) on contracts and non-obvious functions.
  Explain Arc-specific behaviour where it matters — the USDC dual decimal views,
  sub-second finality, the runtime blocklist.
- **This contract only ever touches a 6-decimal ERC-20 view.** USDC is an issue's
  default denomination and EURC is the European publisher's option; both use 6
  decimals. `setIssueToken` refuses any token whose `decimals()` is not 6, so the
  native 18-decimal view can never enter the contract. Native gas accounting is
  never handled in Solidity.
- Tests are Foundry tests (`forge-std/Test.sol`) in `contracts/test/`, with a comment on
  each test explaining the invariant it protects, not what it does. `contracts/lib/` is
  cloned, never vendored.

### Cross-cutting rules

- **USDC has two views of one balance on Arc.** Native gas is 18 decimals; the ERC-20 at
  `0x3600000000000000000000000000000000000000` is 6 decimals. In Python they are
  separate types (`Usdc`, `NativeUsdc`) in
  [domain/money.py](backend/src/misthos/domain/money.py) precisely so they cannot be
  mixed. Everything except raw gas math uses the 6-decimal view. EURC is a *different
  token* with the same 6 decimals, and the escrow holds an issue in one of them, never
  in a sum of both (`setIssueToken`). The backend has no currency of its own yet: every
  amount it holds is USDC, and naming a currency to the gateway is the Arc client's
  change (#69), which is also what widens `ChainGateway` to carry it.
- **The platform never holds customer funds.** Any change that would put money in our
  custody, or enforce an agent limit in application code instead of in the escrow
  contract, breaks a design constraint — raise it rather than implementing it.
- **Lifecycle state has a single writer.** Do not add a second code path that mutates
  issue state (webhook handler included); go through the lifecycle service so every
  transition gets a decision-log entry.
- Anything that hits a blockchain settles against the deployed escrow. Tests inject an
  in-memory chain gateway; shipped code has no offline mode.
- Configuration goes in the relevant `.env.example` with a comment; **never commit a
  real `.env`, key or address-with-funds**. Lockfiles are committed — do not gitignore
  them.

## Branch and commit conventions

- Default branch is `main`. Branch from it and keep it working.
- Branch names are `<type>/<short-slug>`, kebab-case, matching the commit type:
  `feat/escrow-ceiling`, `fix/deadline-refund`, `docs/architecture-components`,
  `chore/lockfile-policy`. One concern per branch.
- Commits follow **Conventional Commits**: `type: imperative subject`, lowercase, no
  trailing period. Allowed types in use: `feat`, `fix`, `docs`, `chore`, `refactor`,
  `test`, `perf`, `build`, `ci`. Keep the subject under ~72 characters and describe the
  behaviour change, not the files touched.
- Rebase on `main` rather than merging it in; the history is linear.
- Squash trivial fixups before opening a PR. A PR should state the behaviour it changes
  and name the tests that cover it.

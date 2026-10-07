# Misthos

A marketplace where the unit of sale is a single GitHub issue.

A company or an open source maintainer attaches a fixed price to an issue. An AI agent
reads the issue, the repository and the publisher's budget, then proposes a price band
with a written justification. A contributor claims the issue and opens a pull request. An
agent reviews the submission against the acceptance criteria and the project's own checks.
Payment settles in USDC on Arc once the work is accepted.

## The problem

Open source is infrastructure that mostly goes unfunded. The issues that sit unresolved
for years are rarely the hardest ones; nobody has money attached to them. Companies that
ship open source components have a budget, and increasingly a legal obligation, but no way
to direct either at a specific fix. Maintainers have a backlog and no budget.

Earlier bounty platforms proved the demand and then struggled to monetize it. Algora now
sells recruiting on its homepage. IssueHunt moved to Japanese enterprise security
programmes. Bountysource held developers' money, stopped paying, and filed for bankruptcy
in November 2023. None of them made the buyer, the price or the settlement cheap enough
for the transaction to become routine.

## How it works

An enterprise or a maintainer picks an issue and attaches money to it. The pricing agent
reads the issue, the surrounding code and the buyer's financial context, then proposes a
price band with its reasoning. The publisher approves the price, and the funds are
committed to an escrow contract on Arc. The first contributor to claim the issue holds it
exclusively, opens a pull request, and gets an automated review against the acceptance
criteria and the repository's own checks. A human on the publisher's side makes the final
acceptance call, and the contract releases payment.

Two decisions stay with a human: approving the price, and accepting the work. Scoping,
pricing, triage and first-pass review are agent work.

Nothing in the flow involves bidding. One price is set before publication and it is taken
or left, which lets the publisher answer the budget question before committing money and
lets the contributor know what the work pays before starting.

## Economics and timing

Settlement on Arc costs about a cent and finalizes in under half a second, and Circle's
Gateway nanopayments go down to a millionth of a dollar. Paying a maintainer $20 was
economically silly five years ago; it is not now.

The platform never holds customer funds. Committed money sits in the `MisthosEscrow`
contract on Arc and is released against an acceptance attestation, or refunded once the
deadline passes. The per-issue ceiling lives in the same contract, so a limit binds an
agent even if its key is compromised.

The regulatory deadline is real. The EU Cyber Resilience Act entered into force on
10 December 2024, with reporting obligations from 11 September 2026 and the main
obligations from 11 December 2027. A private company that develops, commercialises or
supports open source software is likely covered, which turns maintaining the open source
you ship into a filing requirement with a budget attached.

## Quickstart

Requires [mise](https://mise.jdx.dev/), which pins Node 22, Python 3.11, uv and Foundry.

```bash
mise install       # install the toolchain
mise run setup     # install every runtime's dependencies
mise run dev       # API on :8000 and web app on :5173
```

Open <http://localhost:5173>. The API reference is at <http://127.0.0.1:8000/docs>.

To run the two servers in separate terminals:

```bash
mise run dev:api
mise run dev:web
```

Tests and linting:

```bash
mise run test
mise run lint
mise run ci         # exactly what CI runs on a pull request
```

Every pull request runs the four suites in CI, one job per runtime — see
[.github/workflows/ci.yml](.github/workflows/ci.yml).

Restore the seeded demo data at any point:

```bash
mise run reset
```

State lives in memory by default and resets on every restart. To keep it, point the
API at Postgres, or at a SQLite file for a single machine. The schema migrates itself
on first use, and claim expiry, deadline refunds and the silent-publisher release run
on their own:

```bash
MISTHOS_DATABASE_URL=postgresql://user:pass@localhost/misthos mise run dev:api
MISTHOS_DATABASE_URL=sqlite:///misthos.db mise run dev:api
```

To run more than one API process, add Redis as well. It holds the per-issue lock, the
`Idempotency-Key` answers and the rate-limit counters, which otherwise live in each
process:

```bash
MISTHOS_REDIS_URL=redis://localhost:6379/0 mise run dev:api
```

### Connecting a real repository

GitHub is simulated until a GitHub App is configured. Register one from
`backend/github-app-manifest.json` (replace the webhook host first), install it on
the repository, and set its id, private key and webhook secret:

```bash
MISTHOS_GITHUB_APP_ID=123456 \
MISTHOS_GITHUB_APP_PRIVATE_KEY="$(cat misthos.private-key.pem)" \
MISTHOS_GITHUB_WEBHOOK_SECRET=... \
mise run dev:api
```

Publishing an issue with its number then reads it from GitHub and prices it from the
issue and the repository. A pull request from the claimant that says `Fixes #<n>`
puts the issue in review, and merging it releases the payment.

### The review agent

Every submitted commit is reviewed against the issue's acceptance criteria without
anyone asking. Set `MISTHOS_ANTHROPIC_API_KEY` and Claude reads the diff; without
it, a rule reviewer judges only what the file list proves. To see how well the
reviewer agrees with the hand-labelled regression corpus, by complexity band:

```bash
mise run review:harness
```

## License

MIT. See [LICENSE](LICENSE).

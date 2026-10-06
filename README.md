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
```

Restore the seeded demo data at any point:

```bash
mise run reset
```

## License

MIT. See [LICENSE](LICENSE).

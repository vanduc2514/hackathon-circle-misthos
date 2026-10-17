# Misthos

A marketplace where the unit of sale is a single GitHub issue.

A company or an open source maintainer attaches a fixed price to an issue. An AI agent
reads the issue and the repository, then suggests an effort estimate, a range and a
written justification. The suggestion is advice: the publisher names the price, and the
only thing that refuses one is the $5 to $100,000 bound. A contributor claims the issue
and opens a pull request. An agent reviews the submission against the acceptance criteria
and the project's own checks. Payment settles in USDC on Arc once the work is accepted.

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
reads the issue and the surrounding code, then suggests an effort estimate, a range and
its reasoning, as advice the publisher is free to ignore. The publisher types the price,
and it is theirs to move while the issue is unfunded and unclaimed. The funds are
committed to an escrow contract on Arc. The first contributor to claim the issue holds it
exclusively, opens a pull request, and gets an automated review against the acceptance
criteria and the repository's own checks. A human on the publisher's side makes the final
acceptance call, and the contract releases payment.

Two decisions stay with a human: naming the price, and accepting the work. Scoping,
pricing and first-pass review are agent work.

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

Open <http://localhost:5173>. The public API, free and x402 routes with their prices, is
at <http://localhost:5173/public-api>; the full schema is at <http://127.0.0.1:8000/docs>.
To link your real GitHub account rather than a typed login, set up an OAuth App first:
see [Linking your real GitHub account locally](#linking-your-real-github-account-locally).

To run the two servers in separate terminals:

```bash
mise run dev:api
mise run dev:web
```

Tests and linting (the browser test needs Chromium once: `mise run setup:e2e`, then
`mise run test:e2e`, which starts the API and the web app itself):

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

With a database configured, `mise run reset` and the dashboard's Reset button are
refused, because a reset deletes every row in it. Where the database is a demo you
mean to throw away, start the API with `MISTHOS_ALLOW_DEMO_RESET=true` as well.

To run more than one API process, add Redis as well. It holds the per-issue lock, the
`Idempotency-Key` answers and the rate-limit counters, which otherwise live in each
process:

```bash
MISTHOS_REDIS_URL=redis://localhost:6379/0 mise run dev:api
```

### Linking your real GitHub account locally

Publishing and claiming need a linked GitHub login. With nothing configured the
sign-in gate's GitHub step says linking is not set up here. To link your real account:

1. Create a GitHub OAuth App at <https://github.com/settings/developers> (OAuth Apps,
   New OAuth App).
2. Give it the homepage URL `http://localhost:5173` and the authorization callback URL
   `http://localhost:5173/api/v1/auth/github/callback`. The API builds the callback
   from `MISTHOS_PUBLIC_URL`, which defaults to `http://localhost:5173`, and Vite
   forwards `/api` to the API on :8000.
3. Generate a client secret, and put the app's client ID and the secret in a `.env` at
   the root of the repository, which mise loads into every task (`backend/.env` works
   too: the API reads it from `backend/`, where it runs):

   ```bash
   MISTHOS_GITHUB_OAUTH_CLIENT_ID=<the app's client ID>
   MISTHOS_GITHUB_OAUTH_CLIENT_SECRET=<the secret>
   ```

4. Restart `mise run dev`, open <http://localhost:5173> (not 127.0.0.1: GitHub returns
   to the address in `MISTHOS_PUBLIC_URL`, and your session lives on the one you
   opened), press **Sign in**, sign in with a wallet and press **Link with GitHub**.
   GitHub sends you back to the gate, which carries on with the side.

### Connecting a real repository

GitHub is read and written as a GitHub App. Register one from
`backend/github-app-manifest.json` (replace the webhook host first), install it on
the repository, and set its id, private key and webhook secret:

```bash
MISTHOS_GITHUB_APP_ID=123456 \
MISTHOS_GITHUB_APP_PRIVATE_KEY="$(cat misthos.private-key.pem)" \
MISTHOS_GITHUB_WEBHOOK_SECRET=... \
mise run dev:api
```

Publishing an issue with its number then reads it from GitHub and posts the engine's
suggestion. The publisher names the price with `/misthos price 250`; nothing can be
funded until they do. A pull request from the claimant that says `Fixes #<n>`
puts the issue in review, and merging it releases the payment.

Once the publisher who installed the App has signed in and linked that GitHub login,
the repository runs the loop from GitHub alone:

1. Label an issue `misthos` to have it listed, with the engine's suggestion and no price.
2. Comment `/misthos price 250` to name the price.
3. Comment `/misthos approve` to fund it.
4. A contributor comments `/misthos claim`, then opens the pull request.
5. The sweeper reviews it, and the merge pays.

[docs/DEPLOY.md](docs/DEPLOY.md#4-a-real-repository-end-to-end) walks through it
against a real repository.

### Signing in

There is one way into the app: the sign-in gate (#181). Every way in from the landing
page — **Sign in**, **Plans**, an issue's row, **See every issue** — and the app's own
**Sign in** open it while it is unfinished, and it asks for three things in this order:

1. **A wallet.** MetaMask first, or a Circle wallet as the second way in. Either wallet
   signs the
   same Sign-In with Ethereum message (`POST /api/v1/auth/nonce`, then `/auth/verify`),
   and the API recovers the address from the signature the same way for both.
2. **A GitHub account,** linked before the side is chosen (`/auth/github/start`). Until
   the account exists the login is held against
   the wallet, and it is already that wallet's: another wallet linking it, pending or
   complete, is refused with the conflict named.
3. **A side,** publisher or contributor, chosen once (`/auth/role`). This creates the
   account, which adopts the held login. A publisher names its organisation and a
   budget; a contributor is known by its GitHub login.

The gate then closes and the app opens at its overview. Leaving it early costs nothing:
each step is kept by the API as soon as it is done, and reopening the gate starts at the
first step that is not. While it is unfinished the landing page's header says **Finish
signing in** rather than **Open the app**. The profile at `/account` shows who you are,
the wallet the money moves through and a publisher's repositories, and sends you to the
gate for anything missing.

Through the API the three steps are the same for every wallet. `$SIGNED` is the body
`{"message": …, "signature": …}` for that nonce: the message as `siweMessage` in
`frontend/src/lib/siwe.ts` builds it, signed by the wallet it names. GitHub linking is
the OAuth redirect (`/auth/github/start`, then the callback), so it is done in the
browser.

```bash
api=http://localhost:5173/api/v1
nonce=$(curl -s -c jar -X POST $api/auth/nonce | jq -r .nonce)
curl -s -b jar -c jar -X POST $api/auth/verify \
  -H 'Content-Type: application/json' -d "$SIGNED"           # step 1: a session
# step 2: open $api/auth/github/start in the browser and approve on GitHub
curl -s -b jar -X POST $api/auth/role \
  -H 'Content-Type: application/json' -d '{"role": "contributor", "name": "octo-dev"}'
# {"address": "0x…", "role": "contributor", "party_id": "CON-…",
#  "github_login": "octo-dev", "created_at": "…"}
```

A Circle wallet is the same step 1 by another door. `POST /auth/circle/session` with a
random `sign_in_id` the browser keeps returns the Circle user's token and, the first
time, the challenge that sets a PIN and creates the wallet in Circle's frame; once the
wallet exists it returns its address, and `POST /auth/circle/wallet` reads that address
back without asking Circle for anything new. `POST /auth/circle/sign` with the nonce's message
naming that address returns a `SIGN_MESSAGE` challenge for the frame, and the signature
the frame hands back goes to `/auth/verify` like MetaMask's. The route signs only a
sign-in message for this site, with a nonce nobody has spent, naming that very wallet.
The edge service provides it (`MISTHOS_EDGE_CORE_TOKEN`, with `CIRCLE_API_KEY` and
`CIRCLE_APP_ID` on the edge), and `/health` says whether it is set up as
`circle_sign_in`.

Nothing is written anonymously. A publisher acts only on its own
issues, and a contributor submits only a pull request it opened. The loop then runs
through explicit actions:

| Who | Action | Endpoint |
| --- | --- | --- |
| Publisher | Publish an issue and name its price | `POST /issues` |
| Publisher | Name a new price, while the issue is unfunded and unclaimed | `PATCH /issues/{id}/price` |
| Publisher | Approve the acceptance criteria, then the price they named, which commits the funds | `POST /issues/{id}/criteria`, then `/fund` |
| Contributor | Claim the issue, then submit the pull request | `POST /issues/{id}/claim`, then `/submit` |
| Either | Have the review agent judge it now rather than on the sweeper's next pass | `POST /issues/{id}/review` |
| Publisher | Merge on GitHub, which releases the payment | The webhook |

Linking needs a GitHub OAuth App (`MISTHOS_GITHUB_OAUTH_CLIENT_ID` and
`MISTHOS_GITHUB_OAUTH_CLIENT_SECRET`, see
[Linking your real GitHub account locally](#linking-your-real-github-account-locally));
without one linking is not offered. Set `MISTHOS_SESSION_SECRET` to keep sessions
across a restart.

The web app does all of this from the sign-in gate and each issue's page, which shows
each party only the step that is theirs. A wallet extension signs both sides in; the
contributor opens their pull request and the publisher merges it on GitHub. Use two
browsers, or a private window, to be both sides at once.

### Pricing from history

The engine's suggestion moves toward what similar work settled at on the platform, and its
confidence is only as high as that history allows. It is advice: the publisher names the
price, and the only thing that refuses one is the $5 to $100,000 bound. To check the
weights against issues whose worth is known:

```bash
mise run pricing:calibrate                       # the corpus of real, scored issues
```

`--settled` replays the platform's own settlements instead. It is not a check against
known worth: a settled amount is what a publisher paid rather than an independent
valuation, so it measures self-consistency (how far today's engine has moved from the one
that priced those issues), and it fits nothing.

```bash
cd backend && uv run python -m misthos.services.calibration --settled
```

### The whole system

To run the web app, the API, the worker and the edge against Postgres and Redis, the
way it ships:

```bash
docker compose up --build
```

[docs/DEPLOY.md](docs/DEPLOY.md) covers what each service is, the logs and metrics,
and deploying the escrow to Arc testnet.

### The review agent

Every submitted commit is reviewed against the issue's acceptance criteria without
anyone asking. Set `MISTHOS_ANTHROPIC_API_KEY` and a model reads the diff; without
it, a rule reviewer judges only what the file list proves. The key is spent at
`MISTHOS_ANTHROPIC_API_URL`, an Anthropic-compatible endpoint that is DeepSeek's by
default. To see how well the reviewer agrees with the hand-labelled regression
corpus, by complexity band:

```bash
mise run review:harness
```

### The paid API

Agents can buy read-only views of what Misthos knows, one call at a time, over x402:
the edge answers `402` with the price in Arc testnet USDC, and a call paid through
Circle Gateway Nanopayments gets its answer. A call that fails is not charged.
`GET /paid` on the edge lists what is for sale, as a page in a browser.
[docs/PAID-API.md](docs/PAID-API.md) covers how a call is paid, the free tier, and
reconciling the books; [docs/BUYING.md](docs/BUYING.md) how to pay; and
[mcp/](mcp/README.md) is an MCP server that pays for an agent, within a spend cap.

## License

MIT. See [LICENSE](LICENSE).

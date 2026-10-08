# Running and deploying Misthos

Three ways to run it, from least to most real. All three are simulated until you
configure otherwise: no chain is contacted, and GitHub and the review model are
simulated until their keys are set.

## 1. Development: two processes, no infrastructure

```bash
mise run setup
mise run dev
```

The API keeps state in memory and runs the sweeper itself. See the README.

## 2. The whole system: compose

```bash
docker compose up --build
```

| Service | What it is | Reach it at |
| --- | --- | --- |
| `web` | The web app, served by nginx, which proxies `/api`, `/docs` and `/openapi.json` | http://localhost:5173 |
| `api` | FastAPI, with the sweeper switched off | http://127.0.0.1:8000 (localhost only) |
| `worker` | `python -m misthos.workers`: timers, reviews, re-screening, reconciliation | metrics on 9100, inside the network |
| `edge` | The x402 gate and the Circle CLI bridge | http://localhost:8080 |
| `postgres` | State, durable in the `pgdata` volume | inside the network |
| `redis` | The per-issue lock, idempotency keys and rate limits | inside the network |

The schema migrates itself on first start, and an empty database is seeded with the
demo issues. `docker compose down --volumes` starts over. CI boots exactly this and
fails the build if any service does not become healthy.

Pass real integrations through from your shell or a `.env` file next to
`compose.yaml`: `MISTHOS_GITHUB_APP_ID`, `MISTHOS_GITHUB_APP_PRIVATE_KEY`,
`MISTHOS_GITHUB_WEBHOOK_SECRET`, `MISTHOS_ANTHROPIC_API_KEY`, and for sign-in
`MISTHOS_PUBLIC_URL`, `MISTHOS_SESSION_SECRET`, `MISTHOS_GITHUB_OAUTH_CLIENT_ID` and
`MISTHOS_GITHUB_OAUTH_CLIENT_SECRET`. Every other setting is in
`backend/.env.example`.

### Observing it

- Logs are one JSON object per line (`MISTHOS_LOG_JSON=true` in compose). Every line
  carries a `correlation_id`: the `X-Request-ID` nginx assigns to the request, or
  `sweep-…` for a sweeper pass. Money events, verdicts and prices are logged with
  their fields, so `issue_id`, `kind` and `tx_hash` can be filtered on directly.
- Prometheus metrics are served by the API at `/internal/metrics` and by the worker on
  port 9100. Neither is proxied by the web app. The series that matter most are:
  - `misthos_review_seconds` and `misthos_review_cost_usdc` (review in under five
    minutes, for about six dollars)
  - `misthos_price_seconds` (a price in under 60 seconds)
  - `misthos_money_events_total`
  - `misthos_ledger_divergences`, which should always be 0, and
    `misthos_ledger_divergence_alerts_total`. Alert on any increase. Each alert is
    also an `ERROR` line with `"alert": "ledger_divergence"`, and an entry in the
    issue's decision log.

## 3. The contract on Arc testnet

The escrow is deployed with Foundry. Use an encrypted keystore; never put a private
key on the command line.

```bash
cd contracts
mise run setup:contracts
cast wallet import misthos-deployer --interactive
export ARC_TESTNET_RPC_URL=https://rpc.testnet.arc.io
# Optional: the attestor that may release funds; the deployer by default.
export MISTHOS_ATTESTOR_ADDRESS=0x...
forge script script/Deploy.s.sol --rpc-url arc_testnet --account misthos-deployer --broadcast
```

or `mise run contracts:deploy`, which runs the same command. Fund the deployer
with testnet USDC from faucet.circle.com first: USDC is the gas token on Arc.

The script prints the escrow's address. Set it for the API:

```bash
MISTHOS_ESCROW_CONTRACT=0x...   # the address the script printed
MISTHOS_CHAIN=arc-testnet
MISTHOS_RPC_URL=https://rpc.testnet.arc.io
```

The API records it on every commitment today. Settling against it needs the chain
client (#69); until that lands, `MISTHOS_SIMULATED` stays true and the simulated
escrow keeps the books. Report anything settled on testnet as testnet.

Mainnet moves real USDC irreversibly. There is deliberately no mainnet task.

## 4. A real repository, end to end

This runs an issue in a real repository from a label to a payout on GitHub events
(#6). Use a sandbox repository and two GitHub accounts: one publishes, one
contributes. Money stays simulated until the chain client lands (#69), so leave
`MISTHOS_SIMULATED=true`; GitHub is real as soon as the App is configured.

1. **Give GitHub a way to reach the API.** On a laptop, a smee.io channel forwards
   webhooks to it. Open https://smee.io/new, then run:

   ```bash
   npx smee-client --url https://smee.io/<channel> \
     --target http://127.0.0.1:8000/api/v1/webhooks/github
   ```

   A deployment uses its own `https://<host>/api/v1/webhooks/github`.

2. **Register the App** from `backend/github-app-manifest.json`, with
   `hook_attributes.url` set to the smee or deployment URL. It asks for issues,
   pull requests and statuses write, contents and checks read, and the `issues`,
   `issue_comment`, `pull_request` and `check_run` events.
   - Set a webhook secret.
   - Generate a private key, and keep it on your machine. Never put it in a shared
     environment.

3. **Configure the API** and start it:

   ```bash
   MISTHOS_GITHUB_APP_ID=<id> \
   MISTHOS_GITHUB_APP_PRIVATE_KEY="$(cat misthos.private-key.pem)" \
   MISTHOS_GITHUB_WEBHOOK_SECRET=<secret> \
   MISTHOS_GITHUB_APP_SLUG=<slug> \
   mise run dev
   ```

   For real account linking, also set `MISTHOS_GITHUB_OAUTH_CLIENT_ID` and
   `MISTHOS_GITHUB_OAUTH_CLIENT_SECRET` from an OAuth App with the callback
   `http://localhost:5173/api/v1/auth/github/callback`. In the simulation, linking
   by login works too.

4. **The publisher:**
   - Signs in at http://localhost:5173/account, chooses publisher, and links the
     GitHub account they will install the App with.
   - Installs the App on the sandbox repository. **Your repositories** on the
     account page lists it.

5. **On GitHub:**
   1. Open an issue and add the `misthos` label. The App replies with the price,
      the band, the reasoning and the drafted criteria.
   2. The publisher comments `/misthos approve`, or `/misthos criteria` and a list
      first; both can go in one comment, each on its own line, and they run in
      order. The App posts the funded price and criteria.
   3. The contributor signs in once, links their GitHub account, and comments
      `/misthos claim`.
   4. The contributor opens a pull request that says `Fixes #<n>`. The project's
      checks run, and the sweeper's next pass (`MISTHOS_SWEEP_INTERVAL_SECONDS`)
      after they report posts the review. A repository with no checks is reviewed
      on its criteria alone 30 minutes after the pull request is submitted.
   5. The publisher merges. The App posts the settlement, and the issue is `PAID`.

Every step lands in the issue's decision log in the web app.

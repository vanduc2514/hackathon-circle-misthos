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
`MISTHOS_GITHUB_WEBHOOK_SECRET` and `MISTHOS_ANTHROPIC_API_KEY`. Every other setting is
in `backend/.env.example`.

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

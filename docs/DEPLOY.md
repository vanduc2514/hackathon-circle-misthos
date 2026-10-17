# Running and deploying Misthos

Three ways to run it, from least to most real. Every way is live: the API refuses to
start without an escrow deployment and a GitHub App.

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
| `worker` | `python -m misthos.workers`: timers, reviews, re-screening, reconciliation | metrics at http://127.0.0.1:9100/metrics (localhost only), `worker:9100` inside the network |
| `edge` | The x402 seller (the paid API, [PAID-API.md](./PAID-API.md)) and the Circle CLI bridge | http://localhost:8080 |
| `postgres` | State, durable in the `pgdata` volume | inside the network |
| `redis` | The per-issue lock, idempotency keys and rate limits | inside the network |

The schema migrates itself on first start; a fresh database starts empty.
`docker compose down --volumes` starts over. CI boots exactly this and fails the
build if any service does not become healthy.

Pass real integrations through from your shell or a `.env` file next to
`compose.yaml`: `MISTHOS_GITHUB_APP_ID`, `MISTHOS_GITHUB_APP_PRIVATE_KEY`,
`MISTHOS_GITHUB_WEBHOOK_SECRET`, `MISTHOS_ANTHROPIC_API_KEY`,
`MISTHOS_ANTHROPIC_API_URL`, and for sign-in `MISTHOS_PUBLIC_URL`,
`MISTHOS_SESSION_SECRET`, `MISTHOS_GITHUB_OAUTH_CLIENT_ID` and
`MISTHOS_GITHUB_OAUTH_CLIENT_SECRET`. Every other setting is in
`backend/.env.example`.

The edge sells x402 calls. It needs `MISTHOS_SELLER_ADDRESS`, the address paid, and
`MISTHOS_EDGE_CORE_TOKEN`, which compose hands to both the edge and the API: the paid
routes run in the API behind that token. `EDGE_PRICES` overrides a route's price and
`EDGE_FREE_API_KEYS` lets a caller through free. `MISTHOS_PAID_PUBLIC_URL` is where
buyers reach the edge from outside, which the web app's API page (`/public-api`) shows
beside the x402 routes. See [PAID-API.md](./PAID-API.md).

### Observing it

- Logs are one JSON object per line (`MISTHOS_LOG_JSON=true` in compose), uvicorn's
  own startup and access lines included. Every line carries a `correlation_id`: the
  `X-Request-ID` nginx assigns to the request, `sweep-…` for a sweeper pass, or `-`
  for a line that belongs to neither, such as startup. Money events, verdicts and
  prices are logged with their fields, so `issue_id`, `kind` and `tx_hash` can be
  filtered on directly.
- Prometheus metrics come from two processes, and each exports only what it writes:
  the API at `/internal/metrics` (`api:8000` inside the network), and the worker at
  `/metrics` on port 9100 (`worker:9100`). Neither is proxied by the web app, and on
  the host both are published on 127.0.0.1 only. Scrape both:

  ```yaml
  scrape_configs:
    - job_name: misthos-api
      metrics_path: /internal/metrics
      static_configs: [{targets: ["api:8000"]}]
    - job_name: misthos-worker
      static_configs: [{targets: ["worker:9100"]}]
  ```

  | Series | Exported by | What it shows |
  | --- | --- | --- |
  | `misthos_ledger_divergences`, `misthos_ledger_divergence_alerts_total` | the worker | Alert here: the gauge above 0, or any increase of the counter |
  | `misthos_sweep_seconds`, `misthos_sweep_failures_total` | the worker | A sweeper pass that slows down or fails |
  | `misthos_review_seconds`, `misthos_review_cost_usdc` | the worker for the sweeper's reviews, the API for one a person asks for | A review in under five minutes, for about six dollars; sum across both jobs |
  | `misthos_money_events_total` | the API for what people do, the worker for what timers do | Sum across both jobs |
  | `misthos_price_seconds` | the API | A price in under 60 seconds |
  | `misthos_http_request_seconds` | the API | Request latency by route |

  The sweeper's series come only from the process that runs the sweeper: the worker
  under compose, and the API itself in development, where
  `MISTHOS_SWEEPER_IN_PROCESS=true`. An API that does not sweep leaves them out
  rather than reporting a 0 it never measured. Each divergence alert is also an
  `ERROR` line with `"alert": "ledger_divergence"` in the worker's log, and an entry
  in the issue's decision log.

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
# Required: where releases pay the platform's take rate. The script refuses to run
# without it, because the platform sets a take rate on every issue and the escrow
# refuses a release that has a rate and nowhere to send it.
export MISTHOS_FEE_RECIPIENT_ADDRESS=0x...
forge script script/Deploy.s.sol --rpc-url arc_testnet --account misthos-deployer --broadcast \
  --verify --verifier blockscout --verifier-url https://explorer.testnet.arc.io/api/
```

or `mise run contracts:deploy`, which runs the same command. Fund the deployer
with testnet USDC from faucet.circle.com first: USDC is the gas token on Arc. Arc's
explorer runs Blockscout, so verifying needs no API key.

A broadcast writes `contracts/deployments/5042002.json`: the escrow's address, its
owner, attestor and USDC, and the block to scan logs from. Commit it; the API reads
the address from it, so there is nothing to copy by hand. A dry run (without
`--broadcast`) writes no record, and the API refuses a record whose address holds
no code. `MISTHOS_ESCROW_CONTRACT` still pins an
address explicitly and wins over the record. `MISTHOS_CHAIN_ID` names the network;
there is no separate label to set.

`GET /api/v1/issues/{id}/escrow` reads a commitment back from the contract with
`eth_call`. Report anything settled on testnet as testnet.

### Settling on Arc

The store settles through the deployed escrow (#69).
Who signs what is fixed by the contract, and the platform never signs for a
publisher:

| Step | Signed by | Key |
| --- | --- | --- |
| Approving a price records it as the escrow's ceiling | the escrow owner | `MISTHOS_OWNER_SECRET_REF` |
| The commitment itself | the publisher's own wallet, in the browser | never the platform's |
| Release on acceptance | the attestor | `MISTHOS_ATTESTOR_SECRET_REF` |
| Refund once the deadline passed | the attestor (anyone may) | `MISTHOS_ATTESTOR_SECRET_REF` |
| A paid escrow attestation or receipt ([PAID-API.md](./PAID-API.md)) | the certificate key, off chain, EIP-191 | `MISTHOS_CERTIFICATE_SECRET_REF` |

The certificate key is a third key and the escrow does not know it: it vouches for what
the escrow holds and can move nothing. It needs no gas. Its address is published at the
edge's `GET /paid` as `certificate_signer`.

Every key is read from the secret store by reference when it signs.
`MISTHOS_SECRET_STORE_DIR` points at a mounted directory with one file per reference,
mode 600, for local runs and anvil; a production deployment supplies its managed
store behind the same `SecretStore` interface. Fund the owner and the attestor with a
little USDC, which is Arc's gas. The owner is the deployer unless ownership was transferred; the
attestor is the address passed as `MISTHOS_ATTESTOR_ADDRESS` at deploy.

A release the chain refuses — a revert, or the RPC unreachable — does not undo the
acceptance: the payout is held and tried again on the sweeper's next pass, and the issue
stays accepted throughout (#125).

The publisher names the price, and it is that price the escrow records as its ceiling. The
engine's reading of the issue -- an effort estimate, a range and the comparables behind it
-- is advice on the page and never bounds what may be published; only the platform's $5
minimum and $100,000 maximum can refuse a number, and the publisher can move it until the
money is committed.

Funding takes two approvals. The first records the ceiling and is refused with
`NotCommitted`; the web app then has the publisher's wallet send the two calls from
`GET /api/v1/issues/{id}/commitment` (the USDC allowance, then the commitment) and
approves again, which books the publisher's own transaction after checking the
amount, the wallet and the deadline against what was approved. The app decides to
hand over by reading `GET /api/v1/issues/{id}/escrow` -- a ceiling with no money
against it means the commitment is owed -- and not by reading the refusal's own
words, which a proxy between the browser and the API may have replaced (#147). A
wallet that has never been to Arc is offered the chain before the two transactions.
After any change to the contract, run `mise run abi:contracts` and commit
`contracts/deployments/MisthosEscrow.abi.json`; `mise run lint` fails while it is
stale.

The script refuses Arc mainnet (chain 5042) unless `MISTHOS_CONFIRM_MAINNET=5042`
is set for that one command. It also refuses to run without
`MISTHOS_FEE_RECIPIENT_ADDRESS`: the platform sets a take rate on every issue, and the
escrow refuses a release that has a rate and nowhere to pay it, so an escrow deployed
without a recipient could take commitments it could never pay out (#127). The API also
reports a missing recipient at startup, and refuses a commitment on such an escrow.

Mainnet moves real USDC irreversibly. There is deliberately no mainnet task.

## 4. A real repository, end to end

This runs an issue in a real repository from a label to a payout on GitHub events
(#6). Use a sandbox repository and two GitHub accounts: one publishes, one
contributes. Money settles on Arc testnet as section 3 describes.

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
   `MISTHOS_GITHUB_OAUTH_CLIENT_SECRET`. These are the App's own **Client ID** and
   **Client secrets** from the same settings page, so no separate OAuth App is
   needed; the callback is `http://localhost:5173/api/v1/auth/github/callback` (and
   the deployment's own `https://<host>/api/v1/auth/github/callback`).

   An OAuth App is not a substitute for the App itself. It can only identify who
   someone is — the token this flow returns is read once for the login and dropped —
   and GitHub delivers webhooks to Apps alone, so a repository's `issues`,
   `issue_comment`, `pull_request` and `check_run` events, which is the whole loop,
   would never arrive.

4. **The publisher:**
   - Presses **Sign in** at http://localhost:5173, signs in with a wallet, links the
     GitHub account they will install the App with, and chooses publisher.
   - Installs the App on the sandbox repository. **Your repositories** on the
     profile at `/account` lists it.

5. **On GitHub:**
   1. Open an issue and add the `misthos` label. The App replies with what comparable
      work settled for, the reasoning and the drafted criteria, and asks the publisher
      to name the price with `/misthos price 250`. Nothing is funded until they do.
      Or publish it from the web app instead: **Publish** lists the repositories the
      App is installed on and the open issues in the chosen one. Picking an issue
      lists it and adds the `misthos` label to it on GitHub. **Create new issue**
      opens a new issue in that repository with the title, description and labels
      typed there. Either way the publisher names the price.
   2. The publisher comments `/misthos price 250` to name the amount if the web form did
      not, then `/misthos approve`, or `/misthos criteria` and a list first; all of them
      can go in one comment, each on its own line, and they run in order. The App posts
      the funded price and criteria.
   3. The contributor signs in once, links their GitHub account, and comments
      `/misthos claim`.
   4. The contributor opens a pull request that says `Fixes #<n>`. The project's
      checks run, and the sweeper's next pass (`MISTHOS_SWEEP_INTERVAL_SECONDS`)
      after they report posts the review. A repository with no checks is reviewed
      on its criteria alone 30 minutes after the pull request is submitted.
   5. The publisher merges. The App posts the settlement, and the issue is `PAID`.

Every step lands in the issue's decision log in the web app.

## 5. The pull-request review

[backend/src/misthos/services/pr_review.py](../backend/src/misthos/services/pr_review.py)
reviews every open pull request in this repository against `main`, posts the review with
an explicit decision — an approving review, or a request for changes — and squash-merges
the ones it approved, and only those. Nothing schedules it: it is run by hand.

The decision is a tool rather than shell written into a workflow so that it can be
tested: nothing merges on a request-changes verdict, a draft, a conflict, a check that
is red or still running, or a pull request another reviewer has held back. A pull
request it cannot review — a diff past the size it reads, an answer it cannot parse — is
skipped and said so in the run's log, never guessed at. Each run reviews all of them,
and one whose head has not moved since the last review is only judged for the merge.

Run it by hand:

```bash
GH_TOKEN=$(gh auth token) MISTHOS_ANTHROPIC_API_KEY=... mise run pr:review
mise run pr:review -- --dry-run     # judge and report; post nothing, merge nothing
mise run pr:review -- --only 117    # one pull request
```

`MISTHOS_ANTHROPIC_API_KEY` pays for the model that writes the reviews. `GH_TOKEN` can
review and merge other people's pull requests, but a pull request this repository's
owner wrote cannot be *approved* by its author, so merging those needs a token whose
user may bypass the `main` ruleset. Without it they are reviewed, given a verdict in a
comment, and left for a person to merge.

The key is spent at `MISTHOS_ANTHROPIC_API_URL`, any Anthropic-compatible endpoint. It
is unset by default, so the backend's built-in one is used; set it when the key belongs
somewhere else, such as `https://api.anthropic.com`.

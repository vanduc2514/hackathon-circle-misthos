# The paid API

Misthos sells read-only views of what it already knows to agents, one call at a time,
over [x402](https://x402.org). An agent asks, is told the price, signs a USDC
authorisation, asks again, and gets the answer. Nobody signs up, nobody holds an API
key, and the buyer pays no gas. This is Path B in [ARCHITECTURE.md](./ARCHITECTURE.md)
(ADR-2): metered calls, settled through Circle Gateway Nanopayments on Arc. The work
payments for issues stay on Path A, the escrow, and nothing here touches it.

The epic is #200. This page grows with it: each phase adds its routes to the
[catalog](#the-catalog).

## One paid call

```mermaid
sequenceDiagram
    participant A as Agent
    participant E as Edge
    participant G as Circle Gateway
    participant C as Core
    A->>E: GET /paid/...
    E-->>A: 402, PAYMENT-REQUIRED
    A->>E: GET /paid/... with PAYMENT-SIGNATURE
    E->>G: verify
    E->>C: /api/v1/internal/paid/... (edge token)
    C-->>E: the answer
    E->>G: settle
    E->>C: POST /internal/paid/calls (the settlement)
    E-->>A: 200, the answer, PAYMENT-RESPONSE
```

1. **Unpaid, the answer is 402.** The `PAYMENT-REQUIRED` header is base64 JSON,
   `x402Version: 2`, naming the resource and every way to pay. The body says the same
   thing for a reader:

   ```json
   {
     "error": "payment_required",
     "route": "decisions",
     "price": { "usdc": "0.01", "base_units": "10000" },
     "accepts": [{
       "scheme": "exact",
       "network": "eip155:5042002",
       "asset": "0x3600000000000000000000000000000000000000",
       "amount": "10000",
       "payTo": "0x<the seller>",
       "maxTimeoutSeconds": 604900,
       "extra": { "name": "GatewayWalletBatched", "version": "1", "verifyingContract": "0x<Gateway Wallet>" }
     }]
   }
   ```

   The asset and the Gateway Wallet come from Gateway's own list of what it supports,
   so they are never typed in by hand.

2. **Verify, then work, then settle.** The edge verifies the signed payment with the
   facilitator, which moves nothing. It then asks the core for the answer. Only if the
   core answered does it settle. A call that fails is never charged, and the buyer may
   retry it with the same signature, because nothing was taken. If settlement fails,
   the answer is withheld: `402 settlement_failed`, `charged: false`.

   Circle's `createGatewayMiddleware` settles before the handler runs, so a handler
   that then fails has already been paid. That is why the edge drives the facilitator
   one step at a time instead.

3. **A paid answer carries `PAYMENT-RESPONSE`**: base64 JSON with the transaction, the
   network and the payer.

4. **The same signature twice gets the first answer.** Within 15 minutes the edge
   answers a repeated `PAYMENT-SIGNATURE` from memory, marked `X-Misthos-Replayed:
   true`: the work does not run again and the payment is not taken again. Two copies
   arriving at once run once. A signature that already bought a different request is
   refused, `409 payment_already_used`. After 15 minutes Gateway itself refuses a spent
   authorisation.

5. **Every settlement is written down.** The edge logs one JSON line,
   `{"event":"x402.settled", route, request, payer, network, amount, asset,
   transaction}`, and posts the same to the core, which keeps the ledger of paid calls
   (`paid_calls`, migration `0019`). See [reconciliation](#reconciliation).

### Long work

A route marked async (the pre-flight review, #204) verifies the payment, queues the
job, and answers `202` with `job_id`, a `receipt` and `poll`. Polling
`GET {route}/jobs/{job_id}` with `Authorization: Receipt <receipt>` is free. It answers
`202` while the job runs, and when it is done it settles the payment and returns the
result with `PAYMENT-RESPONSE`, once; after that the job answers `410`. A job that
fails is never settled. The edge holds the verified payment in memory for 24 hours,
well inside Gateway's seven-day window, so a restart loses the payment rather than
charging for a result nobody can collect.

## What the rail cannot do

- **`exact` only.** Gateway settles the `exact` scheme and nothing else, so a price is
  fixed when the 402 names it. There is no `upto`: a call whose cost varies is sold at
  a capped price instead.
- **EOA signatures only.** Nanopayments do not accept ERC-1271, so a smart contract
  account cannot buy. The buyer keeps a Gateway balance and signs from an EOA.
- **Testnet.** Arc testnet (`eip155:5042002`) by default. Mainnet is refused unless
  `EDGE_CONFIRM_MAINNET=5042`, and is out of scope for #200.

## The free tier

A route marked free-tier is free, with no payment, to:

- a caller sending `X-Api-Key` with one of `EDGE_FREE_API_KEYS`; or
- a signed-in session (`Authorization: Bearer <session>`) the core says may use that
  route free. The edge asks the core (`POST /api/v1/internal/paid/access`), which looks
  the session up and applies the route's rule in `services/paid.py`. A route without a
  rule is never free to a session.

Everyone else pays.

## The catalog

`GET /paid` on the edge is the seller page's data, free: the network, the seller, how
to pay, and every route with its id, method, path, price, whether it is free-tier or
async, and an example. `edge/src/catalog.ts` is the allowlist. The edge serves a paid
route only if it is listed there, and anything else under `/paid` is `404
not_a_paid_route`. Nothing wraps `/api/v1/*`.

Every listed route lives under `/paid/`, and its work runs in the core under
`/api/v1/internal/paid/`. The edge refuses at startup a route outside `/paid/`, and
refuses to forward a call whose core path leaves the paid API, however its parameters
are spelled. No paid route reaches an auth, wallet, statement, policy or lifecycle
route; tests in both services walk every route and check.

| Route | Id | Price | Free to | Phase |
| --- | --- | --- | --- | --- |
| `GET /paid/decisions` | `decisions` | $0.02 a page | anyone signed in | #202 |
| `GET /paid/reputation/{contributor_id}` | `reputation` | $0.05 | that contributor | #202 |
| `GET /paid/escrow/{issue_id}/attestation` | `escrow-attestation` | $0.02 | that issue's publisher | #202 |
| `POST /paid/price` | `price` | $0.25 | any publisher | #203 |
| `GET /paid/comparables` | `comparables` | $0.10 | any publisher | #203 |

`/paid/pricing-report`, which charged $0.25 for the free `/api/v1/metrics`, is gone, and
no market report replaces it: the metrics stay free.

A call the core cannot answer is answered with `{"error", "detail", "charged": false}`
and the payment is not taken: `400` for a filter or parameter the route does not know
(an unknown filter is refused, never ignored), `404` for a record that does not exist,
`409` when the record and the chain disagree, `502` when the chain cannot be read, and
`503` when a signing key is not configured.

### Views over what the platform holds (#202)

Raw public reads stay free: `GET /api/v1/decisions` (the newest 500), a contributor's
profile, `GET /api/v1/issues/{id}/escrow`. What these sell is what those do not give.

**`GET /paid/decisions`**: the whole append-only decision log, oldest first, in
`(created_at, id)` order. `limit` is 1 to **200**, the maximum page size, and 50 by
default. Each page is one payment, and `next` is the cursor for the following page,
`null` on the last. Filters: `issue_id`, `actor` (`agent`, `publisher`, `contributor`,
`system`), `action`, `since` and `until` (ISO 8601; a time with no zone is UTC; `since`
is inclusive, `until` exclusive). Decisions name contributors by handle, never by
wallet, as the public log does.

**`GET /paid/reputation/{contributor_id}`**: a contributor's standing taken apart, as
aggregates only. `reputation`, `settled_issues` and `earned` are exactly the standing
the money ledger derives (`services/reputation.py`). On top of them:

- `score`, 0 to 100: `100 * points / (points + 50)`. A record of about five settled
  issues scores 50, and the curve saturates, so a long record counts for more than one
  large issue.
- `percentile`: the share of contributors with any standing whose points are at or
  below this one's, out of `ranked_contributors`; `null` with no standing.
- `signals`: the agent's verdicts on work the contributor holds (accept, rework,
  reject), `acceptance_rate`, `rework_rounds`, `first_pass_settled` (settled with no
  rework), and disputes opened, overturned and upheld. Counted from the contributor's
  own claim on, so an issue that changed hands is not the earlier claimant's record.
- `repos` and `labels`: where the contributor settled work, most first.

No wallet, no handle, and no payout transfer is in the answer.

**`GET /paid/escrow/{issue_id}/attestation`**: a signed certificate of what
`MisthosEscrow` holds for an issue. The core reads the commitment from the chain,
compares it with its own record of the commitment (amount, status, publisher, deadline),
and signs only when they agree; a divergence is `409 escrow_diverges` and is not
charged. The certificate names the issue and its `bytes32` key, the publisher and its
wallet, the amount and take rate, the deadline, the status (`held`, `released`,
`refunded`), the contract, chain and commitment transaction, and when it was read.

```json
{
  "certificate": { "kind": "misthos/escrow-attestation", "version": 1, "issue": { "id": "ISS-1001", "key": "0x…" }, "amount": { "usdc": "762.05", "base_units": 762047999 }, "status": "held", "escrow": { "contract": "0x…", "chain_id": 5042002, "network": "eip155:5042002", "commit_tx": "0x…" }, "read_at": "…" },
  "canonical": "{\"amount\":…}",
  "signature": "0x…",
  "signer": "0x…",
  "scheme": "eip191-personal-sign"
}
```

To check one: recover the signer from `canonical` and `signature` with EIP-191
`personal_sign` (in Python, `Account.recover_message(encode_defunct(text=canonical),
signature=signature)`), compare it with `certificate_signer` on `GET /paid`, and read
the commitment from `escrow.contract` on `escrow.chain_id` by `issue.key` to see it
still holds. `canonical` is the certificate with sorted keys and no whitespace, and it
is the text that was signed.

The certificate key is a third key, kept in the secret store by
`MISTHOS_CERTIFICATE_SECRET_REF` like the escrow's two. The escrow does not know it, so
nothing it signs can move money. Without it the attestation is `503` and not charged.

### Pricing and the history behind it (#203)

**`POST /paid/price`**: what the engine suggests for an issue the caller describes. It
runs exactly what the issue page runs (`Store.price_advice`: read the signals, find the
comparables, propose), on the caller's description, and lists nothing.

```json
{
  "title": "Retry webhook deliveries that time out",
  "body": "Deliveries that time out are dropped. Expected: retried with backoff.",
  "repo": "acme/hooks",
  "labels": ["bug"],
  "signals": { "blast_radius": 4 },
  "compliance_driven": false,
  "prior_attempts": 0
}
```

`title` and `repo` (`owner/name`, or a `github.com` link) are required. `signals` sets
any of the six (`code_surface`, `requirement_clarity`, `test_coverage`,
`dependency_depth`, `prior_attempts`, `blast_radius`, each 1 to 5) over what the
engine reads from the text; anything else in the body, an unknown signal, or one out
of range is `400`. The repository is not fetched, so a signal that would be judged
from its file tree is judged from the text alone; set it if you know better.

The answer: `recommended`, `band.low` and `band.high`, `confidence` (`low`, `medium`,
`high`) and `confidence_reason`, which says plainly when the history is thin or
missing, `complexity_score`, `estimated_hours`, each signal with its score, whether it
was read or given, and why, `comparables_used` and up to 25 `comparables` (each with
its price, settled date, repo, distance and the price it implies), and the
`justification`. It is advice: on Misthos the publisher names the price, and nothing
here bounds it. No publisher's budget, policy or wallet is in it.

Every quote is kept with what was asked (`price_quotes`, migration `0020`), for audit
and for calibrating the engine. It carries the edge's request digest
(`X-Misthos-Request-Digest`), the same one its settlement carries, so a quote joins the
payment that bought it.

**`GET /paid/comparables`**: the settled history the engine compares against.
Filters: `repo`, `labels` (comma separated; a settlement must carry them all), and the
six signals, all of them or none. With signals, the list is the engine's own
comparables for work of that shape (settled within a year, close enough to count,
closest first with same-repository work ahead), each with its distance and implied
price. Without, it is every settlement, newest first. `limit` is 1 to **100**, 20 by
default; follow `next`. An unknown filter, or some signals but not all, is `400`.

## The core's side

The core's paid routes are internal: every request must carry `X-Misthos-Core-Token`
equal to `MISTHOS_EDGE_CORE_TOKEN`, and anything else is answered `404`, as if nothing
were there. They are not in the API schema. The edge tells the core who a call is for
in `X-Misthos-Paid-Route`, `X-Misthos-Payer`, and `X-Misthos-Free-Caller`
(`role:party_id` or `api-key`).

The core's address never reaches a buyer: `/health` does not name it, and an
unreachable core is `502 core_unavailable` with nothing else.

## Reconciliation

Circle Gateway is the authority on what was paid, as the chain is for the escrow.
`GET /api/v1/internal/paid/calls` (edge token; `since`, `route`, `limit` up to 5000)
returns the settled calls, newest first, the total per route and network, and the
grand total. Compare the total with what Gateway credited the seller over the same
period. A settlement the books lack shows up as an `x402.settled` log line with no row,
and the edge logs `x402.unrecorded` when the core refused to record one.

## Configuration

On the edge (`edge/.env.example`):

| Variable | What it is |
| --- | --- |
| `MISTHOS_SELLER_ADDRESS` | The EVM address paid. Required |
| `GATEWAY_FACILITATOR_URL` | Defaults to the testnet facilitator; the SDK's own default is mainnet |
| `GATEWAY_NETWORKS` | CAIP-2 networks accepted, comma separated. Arc testnet by default |
| `EDGE_CORE_TOKEN` | Shared with the core's `MISTHOS_EDGE_CORE_TOKEN`. Required once any paid route is listed |
| `EDGE_PRICES` | JSON of route id to USDC price, overriding the catalog, such as `{"decisions":"0.02"}`. An unknown id or a price that is not a positive USDC amount stops the edge starting |
| `EDGE_FREE_API_KEYS` | Comma separated, each at least 16 characters |

On the core, `MISTHOS_EDGE_CORE_TOKEN`; without it every internal paid route is `404`.
And `MISTHOS_CERTIFICATE_SECRET_REF`, the certificate key's name in the secret store;
without it the signed routes are `503`.

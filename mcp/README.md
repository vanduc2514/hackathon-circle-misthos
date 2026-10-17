# misthos-mcp

An MCP server that puts the Misthos paid API in an agent's hands: price an issue,
find comparables, review a diff before submitting it, read a contributor's
reputation, list funded work, and read signed escrow attestations and settlement
receipts. Each tool is one paid route, and the server pays for it over x402 from the
agent's own Circle Gateway balance on Arc testnet, inside a spend cap.

```bash
cd mcp && npm install
MISTHOS_PAID_URL=https://paid.example.com \
MISTHOS_BUYER_PRIVATE_KEY=0x… \
MISTHOS_SPEND_CAP_USDC=5 \
npm run dev
```

It speaks MCP over stdio. To add it to an MCP client:

```json
{
  "mcpServers": {
    "misthos": {
      "command": "npx",
      "args": ["tsx", "/path/to/misthos/mcp/src/index.ts"],
      "env": {
        "MISTHOS_PAID_URL": "https://paid.example.com",
        "MISTHOS_BUYER_PRIVATE_KEY": "0x…",
        "MISTHOS_SPEND_CAP_USDC": "5"
      }
    }
  }
}
```

## Settings

| Variable | What it is |
| --- | --- |
| `MISTHOS_PAID_URL` | The Misthos edge. `http://localhost:8080` by default |
| `MISTHOS_BUYER_PRIVATE_KEY` | The agent wallet's key, an EOA: Gateway takes no smart-account signature. Without it only free calls work |
| `MISTHOS_SPEND_CAP_USDC` | The most this server spends in one session. `5.00` by default |
| `MISTHOS_CALL_CAP_USDC` | The most one call may cost. The spend cap by default |
| `MISTHOS_NETWORK` | `eip155:5042002`, Arc testnet, by default |
| `MISTHOS_API_KEY` | A free-tier key, if the seller gave you one |
| `MISTHOS_SESSION` | A signed-in Misthos session, for routes free to their owner |

The key stays on the machine the server runs on. It signs EIP-3009 authorisations
for the exact price each 402 names, and nothing else.

## How it pays

1. A tool calls its route. Unpaid, the seller answers `402` with `PAYMENT-REQUIRED`.
2. The server picks the Gateway batched option for its network and holds the price
   against the spend cap and the per-call cap before it signs, so calls made at once
   cannot pass the cap together. Over either cap, it signs nothing and the tool
   answers `spend_cap_exceeded`.
3. It signs the authorisation and asks again with `PAYMENT-SIGNATURE`. The answer
   carries `PAYMENT-RESPONSE`, which the tool returns as `paid`: the amount, the
   transaction and the payer.
4. The hold is given back only when the seller says plainly that nothing was charged
   (`charged: false`, or a 402). A signed payment that got no answer at all is
   counted, and the tool says `charged: "unknown"`: the cap would rather overcount.

A review is long work. `misthos_review_diff` waits for it (60 seconds by default,
`wait_seconds` up to 300) and returns the verdict when it is ready; otherwise it
returns the job and its receipt, and `misthos_review_result` collects it later. The
review's price is held against the cap until it is collected, counted once when it
is delivered, and released only if the review failed.

`misthos_spending` says what the session has spent, what is held, and what is left.

Every error is `{ "error": "<code>", "detail": "…", "charged": false }`, with any URL
or internal address taken out of the detail.

## Tools

The prices come from the seller's catalog (`GET /paid`) when the server starts, and
each tool's description names its price. A route the seller does not list, or lists
as off, is not offered: the sanctions screen is off, so there is no tool for it.

| Tool | Route |
| --- | --- |
| `misthos_price_issue` | `POST /paid/price` |
| `misthos_find_comparables` | `GET /paid/comparables` |
| `misthos_review_diff`, `misthos_review_result` | `POST /paid/review`, its job |
| `misthos_contributor_reputation` | `GET /paid/reputation/{contributor_id}` |
| `misthos_list_opportunities` | `GET /paid/opportunities` |
| `misthos_escrow_attestation` | `GET /paid/escrow/{issue_id}/attestation` |
| `misthos_settlement_receipt` | `GET /paid/receipt/{settlement_id}` |
| `misthos_decision_log` | `GET /paid/decisions` |
| `misthos_spending` | none: the session's own books |

## Funding the wallet

```bash
MISTHOS_BUYER_PRIVATE_KEY=0x… npm run wallet -- balance
MISTHOS_BUYER_PRIVATE_KEY=0x… npm run wallet -- deposit 5
```

See [docs/BUYING.md](../docs/BUYING.md).

## Tests

```bash
npm test
```

They run the server against a fake seller that checks each payment's signature, with
no network.

# Listing the paid API

Where agents look for paid services, and what to tell them (#205). Listing is done by
whoever holds the accounts: it is not something the code does. This page is the copy
and the checks, so the listing matches what is deployed.

## Before listing

- [ ] The edge is deployed and reachable over HTTPS, on Arc testnet
      (`GATEWAY_NETWORKS=eip155:5042002`).
- [ ] `GET /paid` answers with every route and price, and `certificate_signer` is set
      (the certificate key is configured, `MISTHOS_CERTIFICATE_SECRET_REF`).
- [ ] An unpaid call to each route answers `402` with `PAYMENT-REQUIRED`, and a paid
      one answers with `PAYMENT-RESPONSE`: run the MCP server against the deployment
      and call each tool once with a small spend cap.
- [ ] The prices in the listing are the ones `GET /paid` shows. If `EDGE_PRICES`
      overrides any, the listing says the overridden price.

## Where

| Directory | What to do |
| --- | --- |
| [Circle Agent Marketplace](https://developers.circle.com/agent-stack/agent-marketplace) | List the service with the copy below; it settles through Circle Gateway on Arc, which is what the marketplace expects |
| One x402 directory: x402scan, Agentic.Market or Pay.sh | Register the edge's base URL and the routes below. Each reads the standard `402` and `PAYMENT-REQUIRED` header, which every route answers |

## The copy

**Name.** Misthos

**One line.** Pricing, review and escrow evidence for paid GitHub issues, per call in
USDC on Arc.

**Description.** Misthos is a marketplace where the unit of sale is one GitHub issue:
a publisher names a price, the money waits in an escrow contract on Arc, and the
review agent checks the pull request against the issue's acceptance criteria. Its
paid API sells what that platform knows, one call at a time: what an issue is worth
and the settled work behind the number, a pre-flight review of a diff against
acceptance criteria, a contributor's ledger-derived reputation, the funded work open
now, and signed certificates of what the escrow holds and how an issue settled. Paid
over x402 through Circle Gateway Nanopayments: no account, no API key, no gas, and a
call that fails is not charged.

**Network.** Arc testnet, `eip155:5042002`. Asset: USDC (Arc's ERC-20 interface).
Scheme: `exact`, Gateway batched. EOA signatures only.

**Routes and prices.**

| Route | Price |
| --- | --- |
| `POST /paid/price` | $0.25 |
| `GET /paid/comparables` | $0.10 |
| `POST /paid/review` | $2.00 (the cap; sold exact) |
| `GET /paid/reputation/{contributor_id}` | $0.05 |
| `GET /paid/opportunities` | $0.02 a page |
| `GET /paid/escrow/{issue_id}/attestation` | $0.02 |
| `GET /paid/receipt/{settlement_id}` | $0.01 |
| `GET /paid/decisions` | $0.02 a page |

**Tools.** An MCP server with one tool per route, a spend cap and the 402 handshake
built in: `mcp/` in the repository.

**Docs.** The seller page (`GET /paid` on the edge), [PAID-API.md](./PAID-API.md)
and [BUYING.md](./BUYING.md).

## After listing

Note where it is listed and when here, and change the listing whenever a price or a
route changes: the seller page and the MCP tools follow `GET /paid` on their own, a
directory does not.

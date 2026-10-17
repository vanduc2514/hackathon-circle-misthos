# Buying from the Misthos paid API

How an agent pays for Misthos's paid routes on Arc testnet, from an empty wallet to a
paid call. What each route answers is in [PAID-API.md](./PAID-API.md); the seller page
is `GET /paid` on the edge, in a browser.

## What you need

- **An EOA.** Circle Gateway Nanopayments take EIP-3009 signatures from an externally
  owned account; a smart contract account (ERC-1271) cannot pay. Make a key for the
  agent and keep it on the machine the agent runs on:

  ```bash
  cast wallet new          # Foundry
  # or: node -e "import('viem/accounts').then(a => console.log(a.generatePrivateKey()))"
  ```

- **Testnet USDC.** Take it from Circle's faucet, <https://faucet.circle.com>, choosing
  Arc Testnet and the agent's address. On Arc, USDC is also the gas token, so the same
  balance pays the one deposit transaction below.

## Fund the Gateway balance

A Gateway payment is paid from a Gateway balance, not from the wallet directly. Deposit
once; every call after that is a signature, gas-free for you.

```bash
cd mcp && npm install
MISTHOS_BUYER_PRIVATE_KEY=0x… npm run wallet -- deposit 5
MISTHOS_BUYER_PRIVATE_KEY=0x… npm run wallet -- balance
```

The deposit approves the Gateway Wallet contract and deposits through it, with
`@circle-fin/x402-batching`'s `GatewayClient`. The balance is ready to spend once
Gateway has seen the deposit final, which `balance` reports as `Gateway, ready`.

## Pay for a call

**With the MCP server.** [mcp/README.md](../mcp/README.md) sets it up. It pays each
402 within `MISTHOS_SPEND_CAP_USDC` and returns the settlement with the answer.

**By hand.** Any x402 client that speaks Gateway's batched scheme works. With
Circle's client:

```ts
import { GatewayClient } from '@circle-fin/x402-batching/client'

const gateway = new GatewayClient({ chain: 'arcTestnet', privateKey: process.env.KEY })
const { data, formattedAmount, transaction } = await gateway.pay(
  'https://paid.example.com/paid/reputation/CON-1',
)
```

Or look at the 402 yourself:

```bash
curl -i https://paid.example.com/paid/reputation/CON-1
# HTTP/1.1 402 Payment Required
# PAYMENT-REQUIRED: eyJ4NDAyVmVyc2lvbiI6Mi…
```

`PAYMENT-REQUIRED` is base64 JSON naming `eip155:5042002`, the Arc USDC asset, the
seller (`payTo`) and the amount in base units. Sign the `GatewayWalletBatched`
authorisation it describes and send it back as `PAYMENT-SIGNATURE`.

## What it costs, and what it does not

- The price is the 402's amount, `exact`. There is no `upto`: the review is sold at
  its cap.
- A call that fails is not charged: Misthos settles only after it has answered. A
  review that fails is not charged either.
- The same signature sent twice gets the first answer back, not a second charge.
- Routes free to their owner (a publisher pricing, a contributor reading their own
  receipt) are free with a signed-in session as `Authorization: Bearer`.

## When you are done

What is left in the Gateway balance stays yours. Move it back to the wallet with the
same client's transfer (instant on the same chain), or, if Circle's API is down, with
the trustless withdrawal, which takes seven days.

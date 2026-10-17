#!/usr/bin/env node
/**
 * Fund the agent wallet the MCP server pays from, on Arc testnet (#205).
 *
 *   MISTHOS_BUYER_PRIVATE_KEY=0x… npx tsx src/wallet.ts balance
 *   MISTHOS_BUYER_PRIVATE_KEY=0x… npx tsx src/wallet.ts deposit 5
 *
 * Gateway pays from a Gateway balance, not from the wallet itself: a deposit moves
 * USDC from the wallet into the Gateway Wallet contract once, and every call after
 * that is a signature, with no gas. Get testnet USDC first from Circle's faucet
 * (https://faucet.circle.com, Arc Testnet); on Arc, USDC is also the gas the deposit
 * spends. See docs/BUYING.md.
 */

import { GatewayClient } from '@circle-fin/x402-batching/client'
import type { Hex } from 'viem'

async function main(argv: string[]) {
  const key = process.env.MISTHOS_BUYER_PRIVATE_KEY?.trim()
  if (!key || !/^0x[0-9a-fA-F]{64}$/.test(key)) {
    throw new Error('set MISTHOS_BUYER_PRIVATE_KEY to the agent wallet key (0x and 64 hex)')
  }
  const gateway = new GatewayClient({ chain: 'arcTestnet', privateKey: key as Hex })
  const [command, amount] = argv
  if (command === 'deposit') {
    if (!amount || !/^\d+(\.\d{1,6})?$/.test(amount)) throw new Error('deposit takes an amount in USDC, such as 5')
    const done = await gateway.deposit(amount)
    console.log(`deposited ${done.formattedAmount} USDC from ${done.depositor} in ${done.depositTxHash}`)
  } else if (command !== 'balance') {
    throw new Error('use: wallet.ts balance | wallet.ts deposit <usdc>')
  }
  const balances = await gateway.getBalances()
  console.log(`wallet ${gateway.address}`)
  console.log(`  in the wallet:  ${balances.wallet.formatted} USDC`)
  console.log(`  Gateway, ready: ${balances.gateway.formattedAvailable} USDC`)
}

main(process.argv.slice(2)).catch((err: unknown) => {
  console.error(`wallet: ${(err as Error).message}`)
  process.exit(1)
})

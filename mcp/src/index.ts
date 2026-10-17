#!/usr/bin/env node
/**
 * The Misthos MCP server: the paid API as tools an agent can call, paid over x402
 * from the agent's own Gateway balance, within a spend cap (#205).
 *
 *   MISTHOS_PAID_URL=https://paid.example.com \
 *   MISTHOS_BUYER_PRIVATE_KEY=0x… MISTHOS_SPEND_CAP_USDC=5 npx tsx src/index.ts
 *
 * It speaks MCP over stdio. See README.md for the settings and docs/BUYING.md for
 * funding the wallet.
 */

import { realpathSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import type { Hex } from 'viem'
import { ARC_TESTNET, Buyer } from './buyer.js'
import { createServer, readCatalog } from './tools.js'

export function configFrom(env: NodeJS.ProcessEnv = process.env) {
  const baseUrl = env.MISTHOS_PAID_URL || 'http://localhost:8080'
  const key = env.MISTHOS_BUYER_PRIVATE_KEY?.trim()
  if (key && !/^0x[0-9a-fA-F]{64}$/.test(key)) {
    throw new Error('MISTHOS_BUYER_PRIVATE_KEY must be a 0x-prefixed 32-byte hex key')
  }
  return {
    baseUrl,
    privateKey: key ? (key as Hex) : undefined,
    network: env.MISTHOS_NETWORK || ARC_TESTNET,
    spendCapUsdc: env.MISTHOS_SPEND_CAP_USDC || '5.00',
    callCapUsdc: env.MISTHOS_CALL_CAP_USDC || undefined,
    apiKey: env.MISTHOS_API_KEY || undefined,
    session: env.MISTHOS_SESSION || undefined,
  }
}

async function main() {
  const config = configFrom()
  const buyer = new Buyer(config)
  const catalog = await readCatalog(config.baseUrl)
  if (!catalog) console.error('misthos-mcp: the seller catalog could not be read; prices are shown at call time')
  const server = createServer({ buyer, catalog })
  await server.connect(new StdioServerTransport())
  console.error(
    `misthos-mcp: ${buyer.address ?? 'no wallet (free calls only)'} on ${buyer.network}, ` +
      `spend cap ${buyer.spend.summary().cap} USDC`,
  )
}

const invoked = (): boolean => {
  try {
    return Boolean(process.argv[1]) && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))
  } catch {
    return false
  }
}

if (invoked()) {
  main().catch((err: unknown) => {
    console.error(`misthos-mcp: ${(err as Error).message}`)
    process.exit(1)
  })
}

/**
 * Fakes for the edge's tests: a facilitator that behaves like Circle Gateway's, a
 * buyer's signed payment, and a core that records what it was asked. Nothing here
 * reaches the network.
 */

import {
  ARC_TESTNET,
  TESTNET_FACILITATOR,
  type Facilitator,
  type GateConfig,
  type PaymentPayload,
  type Requirements,
} from './x402-gate.js'

export const SELLER = '0x1111111111111111111111111111111111111111'
export const BUYER = '0x2222222222222222222222222222222222222222'
/** Arc testnet USDC (the native gas token's ERC-20 interface). */
export const ARC_USDC = '0x3600000000000000000000000000000000000000'
export const GATEWAY_WALLET = '0x0077777d7EBA4688BDeF3E311b846F25870A19B9'

export const LIVE: GateConfig = {
  sellerAddress: SELLER,
  facilitatorUrl: TESTNET_FACILITATOR,
  networks: [ARC_TESTNET],
}

/**
 * A facilitator that behaves like Gateway's: it lists the batched kinds it supports,
 * accepts a payload signed "good" for at least the amount asked, and settles it once.
 */
export function fakeFacilitator(
  opts: {
    settleFails?: string
    /** The next settles throw, as a timeout would. `taken` says whether Gateway had
     * already settled them when the answer was lost. */
    unheard?: { times: number; taken: boolean }
  } = {},
) {
  const calls = { supported: 0, verified: [] as Requirements[], settled: [] as Requirements[] }
  const spent = new Set<string>()
  let unheard = opts.unheard?.times ?? 0
  const facilitator: Facilitator = {
    async getSupported() {
      calls.supported += 1
      return {
        kinds: [
          {
            x402Version: 2,
            scheme: 'exact',
            network: ARC_TESTNET,
            extra: { verifyingContract: GATEWAY_WALLET, assets: [{ symbol: 'USDC', address: ARC_USDC, decimals: 6 }] },
          },
          {
            x402Version: 2,
            scheme: 'exact',
            network: 'eip155:84532',
            extra: { verifyingContract: GATEWAY_WALLET, assets: [{ symbol: 'USDC', address: '0xBASE' }] },
          },
        ],
      }
    },
    async verify(payload: PaymentPayload, requirements: Requirements) {
      calls.verified.push(requirements)
      if (payload.payload.signature !== 'good') return { isValid: false, invalidReason: 'invalid_signature' }
      if (BigInt(String(payload.accepted?.amount ?? '0')) < BigInt(requirements.amount)) {
        return { isValid: false, invalidReason: 'insufficient_amount' }
      }
      return { isValid: true, payer: BUYER }
    },
    async settle(payload: PaymentPayload, requirements: Requirements) {
      calls.settled.push(requirements)
      const nonce = String(payload.payload.nonce)
      if (unheard > 0) {
        unheard -= 1
        if (opts.unheard?.taken) spent.add(nonce)
        throw new Error('socket hang up')
      }
      if (opts.settleFails) {
        return { success: false, errorReason: opts.settleFails, transaction: '', network: requirements.network }
      }
      if (spent.has(nonce)) {
        return { success: false, errorReason: 'nonce_used', transaction: '', network: requirements.network }
      }
      spent.add(nonce)
      return { success: true, payer: BUYER, transaction: `0xbatch-${nonce}`, network: requirements.network }
    },
  }
  return { facilitator, calls }
}

/** What a buyer's client sends: the accepted requirements and a signed authorisation. */
export function signed(
  amount: string,
  opts: { nonce?: string; signature?: string; network?: string; indent?: number } = {},
): string {
  const nonce = opts.nonce ?? 'n-1'
  const payload: PaymentPayload = {
    x402Version: 2,
    accepted: { scheme: 'exact', network: opts.network ?? ARC_TESTNET, amount, payTo: SELLER, asset: ARC_USDC },
    payload: {
      signature: opts.signature ?? 'good',
      nonce,
      authorization: { from: BUYER, to: SELLER, value: amount, nonce },
    },
  }
  return Buffer.from(JSON.stringify(payload, null, opts.indent)).toString('base64')
}

/** One call the fake core received. */
export interface CoreHit {
  method: string
  path: string
  body: unknown
  headers: Record<string, string>
}

/**
 * A core that answers from `answer` and records every call. `answer` returns the
 * status and body; returning nothing answers 200 with `{ ok: true }`.
 */
export function fakeCore(
  answer: (hit: CoreHit) => { status: number; body: unknown } | void = () => undefined,
) {
  const hits: CoreHit[] = []
  return {
    hits,
    core: {
      async call(call: { method: string; path: string }, opts: { body?: unknown; headers?: Record<string, string> } = {}) {
        const hit = { method: call.method, path: call.path, body: opts.body, headers: opts.headers ?? {} }
        hits.push(hit)
        return answer(hit) ?? { status: 200, body: { ok: true } }
      },
    },
  }
}

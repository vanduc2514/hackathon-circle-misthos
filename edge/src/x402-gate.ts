/**
 * The x402 rail: Circle Gateway Nanopayments, driven step by step.
 *
 * The buyer signs an EIP-3009 authorisation off chain, the Gateway facilitator verifies
 * it, screens the buyer and the seller, and settles it in a batch on chain, paying the
 * gas. Circle's `createGatewayMiddleware` does all of that before the handler runs, so a
 * handler that then fails has already been paid for. This module talks to the same
 * facilitator (`BatchFacilitatorClient`) one step at a time instead, so the paid router
 * can verify, do the work, and settle only when the work succeeded (#201).
 *
 * Three facts drive the shape and are easy to get wrong:
 *
 *  1. Nanopayments and x402 batch settlement require EOA signatures. They do not
 *     support ERC-1271, so a smart contract account cannot use this rail.
 *  2. Gateway settles the `exact` scheme only. There is no `upto`: a price is fixed
 *     when the 402 names it, and what the buyer signs is that amount.
 *  3. The SDK defaults to Circle's MAINNET facilitator. The facilitator URL is
 *     therefore always passed explicitly, and it defaults to testnet here.
 */

import { BatchFacilitatorClient } from '@circle-fin/x402-batching/server'

export const ARC_TESTNET = 'eip155:5042002'
export const ARC_MAINNET = 'eip155:5042'
export const TESTNET_FACILITATOR = 'https://gateway-api-testnet.circle.com'
export const MAINNET_FACILITATOR = 'https://gateway-api.circle.com'

/** Gateway's batched scheme: `exact`, against the `GatewayWalletBatched` domain. */
export const SCHEME = 'exact'
const BATCHING_NAME = 'GatewayWalletBatched'
const BATCHING_VERSION = '1'
/** Seven days and a little: how long Gateway keeps a signed authorisation good. */
export const AUTHORISATION_WINDOW_SECONDS = 7 * 24 * 60 * 60 + 100

export interface GateConfig {
  sellerAddress: string
  facilitatorUrl: string
  networks: string[]
}

const EVM_ADDRESS = /^0x[0-9a-fA-F]{40}$/

/**
 * Read the gate's configuration. It refuses to start without a seller address, and
 * refuses mainnet unless `EDGE_CONFIRM_MAINNET` names chain 5042: a confirmation
 * step, not a flag that can be left on by accident.
 */
export function gateConfig(env: NodeJS.ProcessEnv = process.env): GateConfig {
  const facilitatorUrl = env.GATEWAY_FACILITATOR_URL || TESTNET_FACILITATOR
  const networks = (env.GATEWAY_NETWORKS || ARC_TESTNET)
    .split(',')
    .map((n) => n.trim())
    .filter(Boolean)
  const sellerAddress = env.MISTHOS_SELLER_ADDRESS ?? ''

  if (!EVM_ADDRESS.test(sellerAddress)) {
    throw new Error('MISTHOS_SELLER_ADDRESS must be an EVM address to accept payments')
  }
  const mainnet = facilitatorUrl === MAINNET_FACILITATOR || networks.includes(ARC_MAINNET)
  if (mainnet && env.EDGE_CONFIRM_MAINNET !== '5042') {
    throw new Error('mainnet settles real USDC: set EDGE_CONFIRM_MAINNET=5042 to confirm')
  }
  return { sellerAddress, facilitatorUrl, networks }
}

/** One way to pay, as the 402 lists it and as the buyer accepts it. */
export interface Requirements {
  scheme: string
  network: string
  asset: string
  amount: string
  payTo: string
  maxTimeoutSeconds: number
  extra: Record<string, unknown>
}

/** What the buyer sends back, base64 JSON in the `PAYMENT-SIGNATURE` header. */
export interface PaymentPayload {
  x402Version: number
  accepted?: Partial<Requirements>
  payload: Record<string, unknown>
  resource?: { url: string; description: string; mimeType: string }
  extensions?: Record<string, unknown>
}

interface SupportedKind {
  x402Version: number
  scheme: string
  network: string
  extra?: Record<string, unknown>
}

/** The facilitator calls this module makes; `BatchFacilitatorClient` in production. */
export interface Facilitator {
  getSupported(): Promise<{ kinds: SupportedKind[] }>
  verify(
    payload: PaymentPayload,
    requirements: Requirements,
  ): Promise<{ isValid: boolean; invalidReason?: string; payer?: string }>
  settle(
    payload: PaymentPayload,
    requirements: Requirements,
  ): Promise<{ success: boolean; errorReason?: string; payer?: string; transaction: string; network: string }>
}

/** A verified payment, waiting for the work it pays for to succeed. */
export interface Verified {
  payload: PaymentPayload
  requirements: Requirements
  payer: string
}

/** A settled payment: what the `PAYMENT-RESPONSE` header carries and the log records. */
export interface Settlement {
  payer: string
  network: string
  amount: string
  asset: string
  transaction: string
  rail: 'gateway-nanopayments'
}

export type VerifyOutcome =
  | { ok: true; verified: Verified }
  | { ok: false; status: 400 | 402; reason: string }

export type SettleOutcome =
  | { ok: true; settlement: Settlement }
  /** `ambiguous` when the facilitator could not be heard: it may have settled. */
  | { ok: false; reason: string; ambiguous?: boolean }

export interface Rail {
  config: GateConfig
  /** Every way to pay `price` (USDC, decimal text) on the configured networks. */
  requirements(price: string): Promise<Requirements[]>
  /** Check a buyer's signed payment against what we charge. Moves no money. */
  verify(header: string, price: string): Promise<VerifyOutcome>
  /** Settle a verified payment. Called only once the work it paid for succeeded. */
  settle(verified: Verified): Promise<SettleOutcome>
}

/** USDC decimal text to the 6-decimal base units x402 carries. */
export function baseUnits(price: string): string {
  if (!/^\d+(\.\d{1,6})?$/.test(price)) throw new Error(`not a USDC amount: ${price}`)
  const [whole, fraction = ''] = price.split('.')
  const units = BigInt(whole) * 1_000_000n + BigInt(fraction.padEnd(6, '0'))
  if (units <= 0n) throw new Error(`a price must be more than zero: ${price}`)
  return units.toString()
}

function usdcOf(kind: SupportedKind): string | null {
  const assets = kind.extra?.assets
  if (!Array.isArray(assets)) return null
  const usdc = assets.find(
    (a): a is { symbol: string; address: string } =>
      typeof a === 'object' && a !== null && (a as { symbol?: unknown }).symbol === 'USDC',
  )
  return usdc?.address ?? null
}

const SUPPORTED_TTL_MS = 10 * 60 * 1000

export function createRail(
  config: GateConfig = gateConfig(),
  facilitator: Facilitator = new BatchFacilitatorClient({
    url: config.facilitatorUrl,
  }) as unknown as Facilitator,
): Rail {
  let supported: { kinds: SupportedKind[]; at: number } | null = null

  async function accepted(): Promise<SupportedKind[]> {
    if (!supported || Date.now() - supported.at > SUPPORTED_TTL_MS) {
      supported = { kinds: (await facilitator.getSupported()).kinds, at: Date.now() }
    }
    return supported.kinds.filter(
      (k) =>
        config.networks.includes(k.network) &&
        typeof k.extra?.verifyingContract === 'string' &&
        usdcOf(k) !== null,
    )
  }

  function requirementsFor(kind: SupportedKind, price: string): Requirements {
    return {
      scheme: SCHEME,
      network: kind.network,
      asset: usdcOf(kind) as string,
      amount: baseUnits(price),
      payTo: config.sellerAddress,
      maxTimeoutSeconds: AUTHORISATION_WINDOW_SECONDS,
      extra: {
        name: BATCHING_NAME,
        version: BATCHING_VERSION,
        verifyingContract: kind.extra?.verifyingContract,
      },
    }
  }

  return {
    config,
    async requirements(price) {
      return (await accepted()).map((kind) => requirementsFor(kind, price))
    },
    async verify(header, price) {
      let payload: PaymentPayload
      try {
        payload = JSON.parse(Buffer.from(header, 'base64').toString('utf-8')) as PaymentPayload
      } catch {
        return { ok: false, status: 400, reason: 'PAYMENT-SIGNATURE is not base64 JSON' }
      }
      const network = payload?.accepted?.network
      if (typeof network !== 'string') {
        return { ok: false, status: 400, reason: 'the payment names no accepted network' }
      }
      const kind = (await accepted()).find((k) => k.network === network)
      if (!kind) return { ok: false, status: 400, reason: `network ${network} is not accepted` }
      // The requirements are ours, built from our price: a payload that accepted a
      // smaller amount fails the facilitator's check against them.
      const requirements = requirementsFor(kind, price)
      try {
        const result = await facilitator.verify(payload, requirements)
        if (!result.isValid) {
          return { ok: false, status: 402, reason: result.invalidReason ?? 'payment not valid' }
        }
        return { ok: true, verified: { payload, requirements, payer: result.payer ?? '' } }
      } catch (err) {
        return { ok: false, status: 402, reason: `verification failed: ${(err as Error).message}` }
      }
    },
    async settle(verified) {
      try {
        const result = await facilitator.settle(verified.payload, verified.requirements)
        if (!result.success) return { ok: false, reason: result.errorReason ?? 'not settled' }
        return {
          ok: true,
          settlement: {
            payer: result.payer ?? verified.payer,
            network: verified.requirements.network,
            amount: verified.requirements.amount,
            asset: verified.requirements.asset,
            transaction: result.transaction,
            rail: 'gateway-nanopayments',
          },
        }
      } catch (err) {
        // A timeout or a reset says nothing about whether Gateway took the payment.
        return { ok: false, reason: (err as Error).message, ambiguous: true }
      }
    },
  }
}

/** The `PAYMENT-REQUIRED` header: base64 JSON, x402 version 2. */
export function paymentRequiredHeader(url: string, description: string, accepts: Requirements[]): string {
  return Buffer.from(
    JSON.stringify({
      x402Version: 2,
      resource: { url, description, mimeType: 'application/json' },
      accepts,
    }),
  ).toString('base64')
}

/** The `PAYMENT-RESPONSE` header: base64 JSON of the settlement. */
export function paymentResponseHeader(settlement: Settlement): string {
  return Buffer.from(
    JSON.stringify({
      success: true,
      transaction: settlement.transaction,
      network: settlement.network,
      payer: settlement.payer,
    }),
  ).toString('base64')
}

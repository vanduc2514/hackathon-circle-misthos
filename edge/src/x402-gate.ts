/**
 * x402 payment gate.
 *
 * A stub, deliberately shaped like the real thing. When this is wired up it
 * should use Circle's Gateway Nanopayments middleware:
 *
 *   npm install @circle-fin/x402-batching @x402/evm
 *
 *   import { x402ResourceServer } from '@x402/express'
 *   import { BatchFacilitatorClient } from '@circle-fin/x402-batching/server'
 *
 *   const server = new x402ResourceServer([
 *     new BatchFacilitatorClient(),   // Gateway nanopayments
 *     new HTTPFacilitatorClient(),     // standard onchain payments
 *   ])
 *
 * Two facts drive the shape and are easy to get wrong:
 *
 *  1. Nanopayments and x402 batch settlement require EOA signatures. They do not
 *     support ERC-1271, so a smart contract account cannot use this rail.
 *  2. The facilitator screens both parties before submitting. Circle's hosted
 *     facilitator validates the EIP-3009 authorisation, screens the buyer and
 *     the seller, submits through a Circle relayer, and pays the settlement gas.
 */

export interface PaymentAccept {
  scheme: 'exact'
  network: string
  maxAmountRequired: string
  resource: string
  description: string
  mimeType: string
  payTo: string
  asset: string
  maxTimeoutSeconds: number
}

export interface PaymentProof {
  raw: string
  resource: string
}

export interface SettlementResult {
  settled: boolean
  payer?: string
  rail?: string
  reason?: string
}

/** Arc testnet. USDC is a fixed predeploy with 6 decimals. */
const ARC_TESTNET = 'eip155:5042002'
const ARC_USDC = '0x3600000000000000000000000000000000000000'
const SELLER = process.env.MISTHOS_SELLER_ADDRESS ?? '0x0000000000000000000000000000000000000000'

/**
 * Build the `accepts` array for a 402 response. An agent reads this, picks a
 * rail it can pay, signs, and retries. No accounts, no API keys.
 */
export function paymentRequirements(resource: string, priceUsdc: string): PaymentAccept[] {
  return [
    {
      scheme: 'exact',
      network: ARC_TESTNET,
      maxAmountRequired: String(BigInt(Math.round(Number(priceUsdc) * 1_000_000))),
      resource,
      description: 'Misthos pricing report',
      mimeType: 'application/json',
      payTo: SELLER,
      asset: ARC_USDC,
      maxTimeoutSeconds: 60,
    },
  ]
}

/**
 * Verify and settle a payment proof.
 *
 * In production this hands the proof to the facilitator and waits for the
 * settlement result. Here it accepts anything non-empty so the flow can be
 * exercised without funds.
 */
export async function verifyAndSettle(proof: PaymentProof): Promise<SettlementResult> {
  if (!proof.raw.trim()) {
    return { settled: false, reason: 'missing payment proof' }
  }
  return {
    settled: true,
    payer: '0xSIMULATED_PAYER',
    rail: 'gateway-nanopayments',
  }
}

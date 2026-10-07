/**
 * x402 payment gate.
 *
 * Live, it is Circle's Gateway Nanopayments middleware from
 * `@circle-fin/x402-batching`: the buyer signs an EIP-3009 authorisation off
 * chain, the Gateway facilitator verifies it, screens the buyer and the seller,
 * and settles it in a batch on chain, paying the gas. Simulated, it answers the
 * same 402 handshake and accepts any non-empty proof, so the flow can be driven
 * without funds and CI never reaches Circle.
 *
 * Two facts drive the shape and are easy to get wrong:
 *
 *  1. Nanopayments and x402 batch settlement require EOA signatures. They do not
 *     support ERC-1271, so a smart contract account cannot use this rail.
 *  2. The middleware defaults to Circle's MAINNET facilitator. The facilitator
 *     URL is therefore always passed explicitly, and it defaults to testnet here.
 */

import type { NextFunction, Request, RequestHandler, Response } from 'express'
import {
  createGatewayMiddleware,
  type GatewayMiddleware,
  type GatewayMiddlewareConfig,
} from '@circle-fin/x402-batching/server'

export const ARC_TESTNET = 'eip155:5042002'
export const ARC_MAINNET = 'eip155:5042'
const ARC_USDC = '0x3600000000000000000000000000000000000000'
export const TESTNET_FACILITATOR = 'https://gateway-api-testnet.circle.com'
export const MAINNET_FACILITATOR = 'https://gateway-api.circle.com'

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

/** What a paid handler learns about the payment, whichever mode settled it. */
export interface Settlement {
  payer: string
  network: string
  amount: string
  rail: 'gateway-nanopayments' | 'simulated'
  transaction?: string
}

export interface GateConfig {
  simulated: boolean
  sellerAddress: string
  facilitatorUrl: string
  networks: string[]
}

const EVM_ADDRESS = /^0x[0-9a-fA-F]{40}$/

/**
 * Read the gate's configuration. Live mode refuses to start without a seller
 * address, and refuses mainnet unless EDGE_CONFIRM_MAINNET names chain 5042:
 * a confirmation step, not a flag that can be left on by accident.
 */
export function gateConfig(env: NodeJS.ProcessEnv = process.env): GateConfig {
  const simulated = (env.MISTHOS_SIMULATED ?? 'true').toLowerCase() !== 'false'
  const facilitatorUrl = env.GATEWAY_FACILITATOR_URL || TESTNET_FACILITATOR
  const networks = (env.GATEWAY_NETWORKS || ARC_TESTNET)
    .split(',')
    .map((n) => n.trim())
    .filter(Boolean)
  const sellerAddress = env.MISTHOS_SELLER_ADDRESS ?? ''

  if (!simulated) {
    if (!EVM_ADDRESS.test(sellerAddress)) {
      throw new Error('MISTHOS_SELLER_ADDRESS must be an EVM address to accept real payments')
    }
    const mainnet = facilitatorUrl === MAINNET_FACILITATOR || networks.includes(ARC_MAINNET)
    if (mainnet && env.EDGE_CONFIRM_MAINNET !== '5042') {
      throw new Error('mainnet settles real USDC: set EDGE_CONFIRM_MAINNET=5042 to confirm')
    }
  }
  return { simulated, sellerAddress, facilitatorUrl, networks }
}

/**
 * The machine-readable price list a simulated 402 returns. Live, the Gateway
 * middleware builds its own from the facilitator's supported networks.
 */
export function paymentRequirements(
  resource: string,
  priceUsdc: string,
  config: GateConfig,
): PaymentAccept[] {
  return config.networks.map((network) => ({
    scheme: 'exact',
    network,
    maxAmountRequired: String(BigInt(Math.round(Number(priceUsdc) * 1_000_000))),
    resource,
    description: 'Misthos pricing report',
    mimeType: 'application/json',
    payTo: config.sellerAddress || '0x0000000000000000000000000000000000000000',
    asset: ARC_USDC,
    maxTimeoutSeconds: 60,
  }))
}

type GatewayFactory = (config: GatewayMiddlewareConfig) => Pick<GatewayMiddleware, 'require'>

export interface Gate {
  config: GateConfig
  /** Middleware that charges `priceUsdc` and leaves the settlement on `res.locals`. */
  require: (priceUsdc: string) => RequestHandler
}

export function settlementOf(res: Response): Settlement {
  return res.locals.settlement as Settlement
}

export function createGate(
  config: GateConfig = gateConfig(),
  factory: GatewayFactory = createGatewayMiddleware,
): Gate {
  if (config.simulated) {
    return { config, require: (price) => simulatedGate(price, config) }
  }

  const gateway = factory({
    sellerAddress: config.sellerAddress,
    networks: config.networks,
    facilitatorUrl: config.facilitatorUrl,
    description: 'Misthos',
  })

  return {
    config,
    require: (price) => {
      const charge = gateway.require(`$${price}`)
      return (req: Request, res: Response, next: NextFunction) => {
        // The middleware verifies and settles before calling next(), and only
        // then is `req.payment` set. A handler never runs on an unsettled payment.
        void charge(req, res, (err?: unknown) => {
          if (err) return next(err)
          const payment = (req as Request & { payment?: Record<string, string> }).payment
          res.locals.settlement = {
            payer: payment?.payer ?? '',
            network: payment?.network ?? '',
            amount: payment?.amount ?? '',
            transaction: payment?.transaction,
            rail: 'gateway-nanopayments',
          } satisfies Settlement
          next()
        })
      }
    },
  }
}

function simulatedGate(priceUsdc: string, config: GateConfig): RequestHandler {
  return (req, res, next) => {
    const proof = req.header('X-PAYMENT') ?? req.header('PAYMENT-SIGNATURE') ?? ''
    if (!proof.trim()) {
      res.status(402).json({
        error: 'payment required',
        accepts: paymentRequirements(req.path, priceUsdc, config),
      })
      return
    }
    res.locals.settlement = {
      payer: '0xSIMULATED_PAYER',
      network: config.networks[0] ?? ARC_TESTNET,
      amount: String(BigInt(Math.round(Number(priceUsdc) * 1_000_000))),
      rail: 'simulated',
    } satisfies Settlement
    next()
  }
}

/**
 * A seller that behaves like the Misthos edge, for the MCP server's tests: 402 with
 * the Gateway batched option, a payment it checks by recovering the signer, answers
 * with `PAYMENT-RESPONSE`, a review job, and an error that names an internal address
 * the buyer must not repeat. Nothing here reaches the network.
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import { getAddress, verifyTypedData, type Hex } from 'viem'

export const SELLER = '0x1111111111111111111111111111111111111111'
export const ARC_USDC = '0x3600000000000000000000000000000000000000'
export const GATEWAY_WALLET = '0x0077777d7EBA4688BDeF3E311b846F25870A19B9'
export const NETWORK = 'eip155:5042002'

const TYPES = {
  TransferWithAuthorization: [
    { name: 'from', type: 'address' },
    { name: 'to', type: 'address' },
    { name: 'value', type: 'uint256' },
    { name: 'validAfter', type: 'uint256' },
    { name: 'validBefore', type: 'uint256' },
    { name: 'nonce', type: 'bytes32' },
  ],
} as const

interface Priced {
  id: string
  method: 'GET' | 'POST'
  path: string
  usdc: string
  answer: (req: IncomingMessage, body: unknown) => { status: number; body: unknown }
  async?: boolean
  available?: boolean
}

const units = (usdc: string) =>
  (BigInt(usdc.split('.')[0]) * 1_000_000n + BigInt((usdc.split('.')[1] ?? '').padEnd(6, '0'))).toString()

export interface FakeSeller {
  url: string
  /** Every call the seller received: method, path, and whether it carried a payment. */
  calls: { method: string; path: string; paid: boolean; payer?: string }[]
  settled: string[]
  jobs: Map<string, { polls: number; receipt: string; fail?: boolean }>
  close(): Promise<void>
}

export async function fakeSeller(routes: Priced[]): Promise<FakeSeller> {
  const calls: FakeSeller['calls'] = []
  const settled: string[] = []
  const jobs: FakeSeller['jobs'] = new Map()
  const spent = new Set<string>()

  const send = (res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}) => {
    res.writeHead(status, { 'Content-Type': 'application/json', ...headers })
    res.end(JSON.stringify(body))
  }

  const server: Server = createServer(async (req, res) => {
    const chunks: Buffer[] = []
    for await (const chunk of req) chunks.push(chunk as Buffer)
    const text = Buffer.concat(chunks).toString()
    const body = text ? JSON.parse(text) : undefined
    const url = new URL(req.url ?? '/', 'http://seller')

    if (url.pathname === '/paid') {
      return send(res, 200, {
        networks: [NETWORK],
        payTo: SELLER,
        accepts: [{ scheme: 'exact', network: NETWORK, asset: ARC_USDC, payTo: SELLER }],
        routes: routes.map((r) => ({
          id: r.id,
          method: r.method,
          path: r.path,
          price: { usdc: r.usdc, base_units: units(r.usdc) },
          async: Boolean(r.async),
          available: r.available !== false,
        })),
      })
    }

    const poll = /^\/paid\/review\/jobs\/([^/]+)$/.exec(url.pathname)
    if (poll) {
      const job = jobs.get(decodeURIComponent(poll[1]))
      calls.push({ method: 'GET', path: url.pathname, paid: false })
      if (!job || req.headers.authorization !== `Receipt ${job.receipt}`) return send(res, 404, { error: 'no_such_job' })
      job.polls += 1
      if (job.fail) {
        jobs.delete(poll[1])
        return send(res, 200, { status: 'failed', charged: false })
      }
      if (job.polls === 1) return send(res, 202, { status: 'running', charged: false })
      if (job.polls === 2) {
        settled.push(poll[1])
        return send(res, 200, { status: 'done', verdict: 'accept' }, {
          'PAYMENT-RESPONSE': Buffer.from(JSON.stringify({ success: true, transaction: `batch-${poll[1]}`, network: NETWORK, payer: '0xbuyer' })).toString('base64'),
        })
      }
      return send(res, 410, { error: 'already_delivered' })
    }

    const route = routes.find((r) => r.method === req.method && new RegExp(`^${r.path.replace(/:\w+/g, '[^/]+')}$`).test(url.pathname))
    if (!route) return send(res, 404, { error: 'not_a_paid_route' })
    const signature = req.headers['payment-signature']
    const option = {
      scheme: 'exact',
      network: NETWORK,
      asset: ARC_USDC,
      amount: units(route.usdc),
      payTo: SELLER,
      maxTimeoutSeconds: 604900,
      extra: { name: 'GatewayWalletBatched', version: '1', verifyingContract: GATEWAY_WALLET },
    }
    if (typeof signature !== 'string') {
      calls.push({ method: req.method ?? '', path: url.pathname, paid: false })
      if (req.headers['x-api-key'] === 'free-key-0123456789') {
        return send(res, 200, route.answer(req, body).body)
      }
      const required = { x402Version: 2, resource: { url: req.url, description: route.id, mimeType: 'application/json' }, accepts: [option] }
      return send(res, 402, { error: 'payment_required', accepts: [option] }, {
        'PAYMENT-REQUIRED': Buffer.from(JSON.stringify(required)).toString('base64'),
      })
    }

    const payment = JSON.parse(Buffer.from(signature, 'base64').toString()) as {
      payload: { signature: Hex; authorization: Record<string, string> }
      accepted: typeof option
    }
    const auth = payment.payload.authorization
    const valid = await verifyTypedData({
      address: getAddress(auth.from),
      domain: { name: 'GatewayWalletBatched', version: '1', chainId: 5042002, verifyingContract: GATEWAY_WALLET },
      types: TYPES,
      primaryType: 'TransferWithAuthorization',
      message: {
        from: getAddress(auth.from),
        to: getAddress(auth.to),
        value: BigInt(auth.value),
        validAfter: BigInt(auth.validAfter),
        validBefore: BigInt(auth.validBefore),
        nonce: auth.nonce as Hex,
      },
      signature: payment.payload.signature,
    })
    calls.push({ method: req.method ?? '', path: url.pathname, paid: true, payer: auth.from })
    if (!valid || auth.value !== option.amount || getAddress(auth.to) !== getAddress(SELLER) || spent.has(auth.nonce)) {
      return send(res, 402, { error: 'payment_required', reason: 'invalid_signature' })
    }
    const out = route.answer(req, body)
    if (out.status >= 400) return send(res, out.status, { ...(out.body as object), charged: false })
    if (route.async) {
      const id = `job-${jobs.size + 1}`
      jobs.set(id, { polls: 0, receipt: `receipt-${id}`, fail: (body as { fail?: boolean })?.fail })
      return send(res, 202, { job_id: id, receipt: `receipt-${id}`, poll: `/paid/review/jobs/${id}`, charged: false })
    }
    spent.add(auth.nonce)
    settled.push(auth.nonce)
    return send(res, out.status, out.body, {
      'PAYMENT-RESPONSE': Buffer.from(JSON.stringify({ success: true, transaction: `batch-${settled.length}`, network: NETWORK, payer: auth.from })).toString('base64'),
    })
  }).listen(0)
  await new Promise((resolve) => server.once('listening', resolve))
  const { port } = server.address() as AddressInfo
  return {
    url: `http://127.0.0.1:${port}`,
    calls,
    settled,
    jobs,
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections()
        server.close(() => resolve())
      }),
  }
}

export const STANDARD: Priced[] = [
  {
    id: 'reputation',
    method: 'GET',
    path: '/paid/reputation/:contributor_id',
    usdc: '0.05',
    answer: (req) => ({ status: 200, body: { contributor_id: decodeURIComponent((req.url ?? '').split('/').pop() ?? ''), score: 42 } }),
  },
  {
    id: 'price',
    method: 'POST',
    path: '/paid/price',
    usdc: '0.25',
    answer: (_req, body) => ({ status: 200, body: { recommended: { usdc: '120.00' }, asked: body } }),
  },
  {
    id: 'review',
    method: 'POST',
    path: '/paid/review',
    usdc: '2.00',
    async: true,
    answer: () => ({ status: 202, body: {} }),
  },
  {
    id: 'escrow-attestation',
    method: 'GET',
    path: '/paid/escrow/:issue_id/attestation',
    usdc: '0.02',
    answer: () => ({ status: 502, body: { error: 'chain_unavailable', detail: 'the Arc RPC at http://10.1.2.3:8545 failed' } }),
  },
  {
    id: 'screen',
    method: 'POST',
    path: '/paid/screen',
    usdc: '0.25',
    available: false,
    answer: () => ({ status: 503, body: { error: 'route_unavailable' } }),
  },
]

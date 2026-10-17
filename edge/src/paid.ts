/**
 * The paid API: every route an agent pays for over x402, and nothing else (#201).
 *
 * Each route is declared once, in `catalog.ts`, with its price, and is forwarded to the
 * core's internal paid routes (`/api/v1/internal/paid/...`), which only this service can
 * call. Nothing here wraps `/api/v1/*`: a path that is not in the catalog is not paid
 * for, and is not served.
 *
 * One call, paid:
 *
 *   1. No `PAYMENT-SIGNATURE`: answer 402 with what we accept and the price.
 *   2. The same signature again: answer what it bought the first time. Work is not
 *      run twice and the payment is not taken twice.
 *   3. Verify the payment with the facilitator. Nothing moves yet.
 *   4. Run the work in the core. If it fails, settle nothing: a failed call is free.
 *   5. Settle, record the settlement, and answer with `PAYMENT-RESPONSE`.
 *
 * A route marked `freeTier` is free to a caller the core recognises (a signed-in web
 * session it says may use that route free) or that carries one of `EDGE_FREE_API_KEYS`.
 * Everyone else pays.
 *
 * A route marked `async` runs long work: the payment is verified, the core queues a
 * job, and the answer is 202 with a job id and a receipt. Polling with the receipt
 * returns the result once, and the payment is settled only then. A job that fails is
 * never charged.
 */

import { createHash, randomBytes, timingSafeEqual } from 'node:crypto'
import { Router, type Request, type Response } from 'express'
import {
  baseUnits,
  paymentRequiredHeader,
  paymentResponseHeader,
  type Rail,
  type Settlement,
  type Verified,
} from './x402-gate.js'

export const CORE_PAID_PREFIX = '/api/v1/internal/paid/'

export interface CoreCall {
  method: 'GET' | 'POST'
  /** Under `CORE_PAID_PREFIX`, with its query string. */
  path: string
}

export interface PaidRoute {
  id: string
  method: 'GET' | 'POST'
  /** The Express path the buyer calls, under `/paid/`. */
  path: string
  /** The default price in USDC, decimal text. `EDGE_PRICES` may override it. */
  price: string
  description: string
  /** Where the work runs in the core. */
  core: (req: Request) => CoreCall
  /** Free to a caller the core recognises for this route, or with a free API key. */
  freeTier?: boolean
  /** Long work: 202 and a job to poll, settled when the result is delivered. */
  async?: boolean
  /** What a call looks like, for the catalog and the MCP tools. */
  example?: { params?: Record<string, string>; query?: Record<string, string>; body?: unknown }
  /** What the call is about, for the settlement log: an issue or a contributor. */
  ref?: (req: Request) => string | undefined
  /** The price is a cap on work whose cost varies. Gateway settles `exact` only, so
   * the cap is what is charged; the 402 and the catalog say so. */
  capped?: boolean
  /** One line for the buyer about how the route is priced or run. */
  note?: string
}

/** The request's own query string, as the buyer sent it, for the core to read and to
 * refuse a filter it does not know. Empty, or `?` and the query. */
export function queryOf(req: Request): string {
  const at = req.originalUrl.indexOf('?')
  return at < 0 ? '' : req.originalUrl.slice(at)
}

/** A path segment from a route parameter, encoded so it cannot leave its segment. */
export function segment(value: unknown): string {
  return encodeURIComponent(String(value ?? ''))
}

/** Answers from the core, as the paid router sees them. */
export interface Core {
  call(
    call: CoreCall,
    opts?: { body?: unknown; headers?: Record<string, string> },
  ): Promise<{ status: number; body: unknown }>
}

/** The core over HTTP, with the shared token. Its address never reaches a buyer. */
export function coreClient(baseUrl: string, token: string, timeoutMs = 60_000): Core {
  const root = baseUrl.replace(/\/+$/, '')
  return {
    async call(call, opts = {}) {
      try {
        const res = await fetch(`${root}${call.path}`, {
          method: call.method,
          headers: {
            'X-Misthos-Core-Token': token,
            ...(opts.body === undefined ? {} : { 'Content-Type': 'application/json' }),
            ...opts.headers,
          },
          body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
          signal: AbortSignal.timeout(timeoutMs),
        })
        const text = await res.text()
        let body: unknown = {}
        try {
          body = text ? JSON.parse(text) : {}
        } catch {
          body = { error: 'core_answered_non_json' }
        }
        return { status: res.status, body }
      } catch {
        return { status: 502, body: { error: 'core_unavailable' } }
      }
    },
  }
}

/** Per-route prices: the catalog's defaults, overridden by `EDGE_PRICES` (JSON). */
export function prices(routes: PaidRoute[], env: NodeJS.ProcessEnv = process.env): Map<string, string> {
  const out = new Map(routes.map((r) => [r.id, r.price]))
  const raw = env.EDGE_PRICES?.trim()
  if (!raw) return out
  let given: unknown
  try {
    given = JSON.parse(raw)
  } catch {
    throw new Error('EDGE_PRICES must be a JSON object of route id to USDC price')
  }
  if (typeof given !== 'object' || given === null || Array.isArray(given)) {
    throw new Error('EDGE_PRICES must be a JSON object of route id to USDC price')
  }
  for (const [id, price] of Object.entries(given)) {
    if (!out.has(id)) throw new Error(`EDGE_PRICES names ${id}, which is not a paid route`)
    if (typeof price !== 'string') throw new Error(`EDGE_PRICES.${id} must be a string`)
    baseUnits(price) // refuses anything that is not a positive USDC amount
    out.set(id, price)
  }
  return out
}

/** Free API keys from `EDGE_FREE_API_KEYS`, comma separated. */
export function freeApiKeys(env: NodeJS.ProcessEnv = process.env): string[] {
  return (env.EDGE_FREE_API_KEYS ?? '')
    .split(',')
    .map((k) => k.trim())
    .filter((k) => k.length >= 16)
}

function sameSecret(a: string, b: string): boolean {
  const left = Buffer.from(a)
  const right = Buffer.from(b)
  return left.length === right.length && timingSafeEqual(left, right)
}

const sha256 = (value: string): string => createHash('sha256').update(value).digest('hex')

/** What a retried payment is answered with: the first answer, exactly. */
interface Bought {
  request: string
  status: number
  body: unknown
  paymentResponse?: string
  at: number
}

/** A short-lived memory of answered payments, keyed by the signature's hash. */
export class ReplayCache {
  private readonly done = new Map<string, Bought>()
  private readonly running = new Map<string, Promise<void>>()

  constructor(
    private readonly ttlMs = 15 * 60 * 1000,
    private readonly max = 5000,
  ) {}

  get(key: string): Bought | undefined {
    const hit = this.done.get(key)
    if (hit && Date.now() - hit.at > this.ttlMs) {
      this.done.delete(key)
      return undefined
    }
    return hit
  }

  set(key: string, bought: Omit<Bought, 'at'>): void {
    if (this.done.size >= this.max) {
      const oldest = this.done.keys().next().value
      if (oldest !== undefined) this.done.delete(oldest)
    }
    this.done.set(key, { ...bought, at: Date.now() })
  }

  /** Run `work` once per key; a second call with the same key waits for the first. */
  async once(key: string, work: () => Promise<void>): Promise<boolean> {
    const already = this.running.get(key)
    if (already) {
      await already
      return false
    }
    const running = work().finally(() => this.running.delete(key))
    this.running.set(key, running)
    await running
    return true
  }
}

/** A verified payment held for a job until its result is delivered. */
interface HeldJob {
  route: string
  request: string
  receipt: string
  verified: Verified
  delivered: boolean
  at: number
}

export interface SettlementLog {
  (entry: { route: string; request: string; settlement: Settlement; ref?: string }): void
}

export interface PaidRouterOptions {
  rail: Rail
  routes: PaidRoute[]
  core: Core
  prices?: Map<string, string>
  apiKeys?: string[]
  replay?: ReplayCache
  /** Where a settlement is written: stdout and the core's ledger of paid calls. */
  log?: SettlementLog
  /** How long a held job's payment stays good: under Gateway's seven-day window. */
  jobTtlMs?: number
}

/** The buyer-facing request digest: a retried payment must be the same request. */
function requestDigest(route: PaidRoute, req: Request): string {
  return sha256(
    JSON.stringify([route.id, req.method, req.originalUrl, req.body === undefined ? null : req.body]),
  )
}

/**
 * True when `path` stays inside the core's paid API: under the prefix, with no `.` or
 * `..` segment once decoded, so no route parameter can walk out of it.
 */
export function insidePaidApi(path: string): boolean {
  if (!path.startsWith(CORE_PAID_PREFIX)) return false
  const [route] = path.split('?')
  let decoded: string
  try {
    decoded = decodeURIComponent(route)
  } catch {
    return false
  }
  return decoded.startsWith(CORE_PAID_PREFIX) && !decoded.split(/[/\\]/).some((seg) => seg === '.' || seg === '..')
}

function errorBody(body: unknown): Record<string, unknown> {
  if (typeof body === 'object' && body !== null && !Array.isArray(body)) {
    return body as Record<string, unknown>
  }
  return { error: 'core_error' }
}

/** Log every settlement to stdout and to the core, which keeps the ledger of paid calls. */
export function settlementLog(core: Core): SettlementLog {
  return ({ route, request, settlement, ref }) => {
    console.log(
      JSON.stringify({ event: 'x402.settled', route, request, ref, ...settlement }),
    )
    void core
      .call(
        { method: 'POST', path: `${CORE_PAID_PREFIX}calls` },
        {
          body: {
            route,
            request_digest: request,
            payer: settlement.payer,
            amount_base_units: Number(settlement.amount),
            network: settlement.network,
            asset: settlement.asset,
            transaction: settlement.transaction,
            ref: ref ?? null,
          },
        },
      )
      .then((r) => {
        if (r.status >= 300) {
          console.error(JSON.stringify({ event: 'x402.unrecorded', route, request, status: r.status }))
        }
      })
  }
}

export function paidRouter(opts: PaidRouterOptions): Router {
  const { rail, routes, core } = opts
  const priced = opts.prices ?? prices(routes)
  const keys = opts.apiKeys ?? []
  const replay = opts.replay ?? new ReplayCache()
  const log = opts.log ?? settlementLog(core)
  const jobTtl = opts.jobTtlMs ?? 24 * 60 * 60 * 1000
  const jobs = new Map<string, HeldJob>()
  const router = Router()

  for (const route of routes) {
    if (!route.path.startsWith('/paid/')) {
      throw new Error(`paid route ${route.id} must live under /paid/`)
    }
    baseUnits(priced.get(route.id) ?? route.price)
  }

  /** Who may call this route free, or null when the caller pays. */
  async function freeCaller(route: PaidRoute, req: Request): Promise<string | null> {
    if (!route.freeTier) return null
    const key = req.header('x-api-key')
    if (key && keys.some((k) => sameSecret(k, key))) return 'api-key'
    const auth = req.header('authorization')
    if (!auth?.toLowerCase().startsWith('bearer ')) return null
    const asked = await core.call(
      { method: 'POST', path: `${CORE_PAID_PREFIX}access` },
      {
        body: { route: route.id, params: req.params, query: req.query },
        headers: { 'X-Misthos-Caller-Authorization': auth },
      },
    )
    const body = asked.body as { free?: unknown; who?: unknown }
    return asked.status === 200 && body.free === true ? String(body.who ?? 'session') : null
  }

  async function paymentRequired(route: PaidRoute, req: Request, res: Response, reason?: string) {
    const price = priced.get(route.id) as string
    let accepts
    try {
      accepts = await rail.requirements(price)
    } catch {
      res.status(503).json({ error: 'payments_unavailable' })
      return
    }
    if (accepts.length === 0) {
      res.status(503).json({ error: 'no_payment_networks' })
      return
    }
    res.setHeader('PAYMENT-REQUIRED', paymentRequiredHeader(req.originalUrl, route.description, accepts))
    res.status(402).json({
      error: 'payment_required',
      ...(reason ? { reason } : {}),
      route: route.id,
      scheme: 'exact',
      price: { usdc: price, base_units: baseUnits(price) },
      ...(route.capped ? { cap: { usdc: price, base_units: baseUnits(price) }, upto: false } : {}),
      ...(route.note ? { note: route.note } : {}),
      ...(route.async ? { async: true } : {}),
      accepts,
    })
  }

  function send(res: Response, bought: Omit<Bought, 'at'>, replayed: boolean) {
    if (bought.paymentResponse) res.setHeader('PAYMENT-RESPONSE', bought.paymentResponse)
    if (replayed) res.setHeader('X-Misthos-Replayed', 'true')
    res.status(bought.status).json(bought.body)
  }

  function forwarded(route: PaidRoute, req: Request, extra: Record<string, string>) {
    const call = route.core(req)
    if (!insidePaidApi(call.path)) {
      // A catalog mistake, not the buyer's: nothing outside the paid API is ever called.
      return Promise.resolve({ status: 500, body: { error: 'not_a_paid_route' } })
    }
    return core.call(call, {
      body: route.method === 'POST' ? req.body : undefined,
      headers: {
        'X-Misthos-Paid-Route': route.id,
        // The digest the settlement will carry, so what the core keeps of a call
        // (a price quote) joins the payment that bought it.
        'X-Misthos-Request-Digest': requestDigest(route, req),
        ...extra,
      },
    })
  }

  /** Verify, run the work, and settle only if it succeeded. */
  async function buy(route: PaidRoute, req: Request, signature: string, request: string) {
    const price = priced.get(route.id) as string
    const checked = await rail.verify(signature, price)
    if (!checked.ok) {
      return checked.status === 400
        ? ({ kind: 'bad_payment', reason: checked.reason } as const)
        : ({ kind: 'pay', reason: checked.reason } as const)
    }
    const out = await forwarded(route, req, { 'X-Misthos-Payer': checked.verified.payer })
    if (out.status >= 400) {
      // The work failed, so the payment is not taken. The buyer may retry with the
      // same signature: it was never settled.
      return { kind: 'failed', status: out.status, body: { ...errorBody(out.body), charged: false } } as const
    }
    if (route.async) {
      const jobId = String((out.body as { job_id?: unknown }).job_id ?? '')
      if (!jobId) {
        return { kind: 'failed', status: 502, body: { error: 'core_did_not_queue_the_job', charged: false } } as const
      }
      const receipt = randomBytes(24).toString('base64url')
      jobs.set(jobId, {
        route: route.id,
        request,
        receipt: sha256(receipt),
        verified: checked.verified,
        delivered: false,
        at: Date.now(),
      })
      return {
        kind: 'bought',
        bought: {
          request,
          status: 202,
          body: {
            ...errorBody(out.body),
            job_id: jobId,
            receipt,
            poll: `${route.path}/jobs/${jobId}`,
            charged: false,
            note: 'the payment is settled when the result is delivered; a failed job is not charged',
          },
        },
      } as const
    }
    const settled = await rail.settle(checked.verified)
    if (!settled.ok) {
      return {
        kind: 'failed',
        status: 402,
        body: { error: 'settlement_failed', reason: settled.reason, charged: false },
      } as const
    }
    log({ route: route.id, request, settlement: settled.settlement, ref: route.ref?.(req) })
    return {
      kind: 'bought',
      bought: {
        request,
        status: out.status,
        body: out.body,
        paymentResponse: paymentResponseHeader(settled.settlement),
      },
    } as const
  }

  /** Ask the core how a held job is doing; settle and deliver it once, when it is done. */
  async function poll(route: PaidRoute, jobId: string, held: HeldJob) {
    const out = await core.call({
      method: 'GET',
      path: `${CORE_PAID_PREFIX}${route.id}/jobs/${encodeURIComponent(jobId)}`,
    })
    const body = errorBody(out.body)
    if (out.status >= 400 || body.status === 'failed') {
      // A failed job is not charged: the held payment is dropped, never settled.
      jobs.delete(jobId)
      return { status: out.status >= 400 ? out.status : 200, body: { ...body, status: 'failed', charged: false } }
    }
    if (body.status !== 'done') return { status: 202, body: { ...body, charged: false } }
    const settled = await rail.settle(held.verified)
    if (!settled.ok) {
      return { status: 402, body: { error: 'settlement_failed', reason: settled.reason, charged: false } }
    }
    held.delivered = true
    log({ route: route.id, request: held.request, settlement: settled.settlement, ref: jobId })
    return { status: 200, body, paymentResponse: paymentResponseHeader(settled.settlement) }
  }

  for (const route of routes) {
    const handle = async (req: Request, res: Response) => {
      const free = await freeCaller(route, req)
      if (free) {
        const out = await forwarded(route, req, { 'X-Misthos-Free-Caller': free })
        res.status(out.status).json(out.body)
        return
      }

      const signature = req.header('payment-signature')
      if (!signature) return paymentRequired(route, req, res)
      const key = sha256(signature)
      const request = requestDigest(route, req)

      const answered = () => {
        const bought = replay.get(key)
        if (!bought) return false
        if (bought.request !== request) {
          res.status(409).json({
            error: 'payment_already_used',
            reason: 'this payment already bought another request; sign a new one',
          })
        } else {
          send(res, bought, true)
        }
        return true
      }
      if (answered()) return

      type Outcome =
        | { kind: 'bought'; bought: Omit<Bought, 'at'> }
        | { kind: 'bad_payment'; reason: string }
        | { kind: 'pay'; reason: string }
        | { kind: 'failed'; status: number; body: unknown }
      const result: { outcome?: Outcome } = {}

      const ran = await replay.once(key, async () => {
        result.outcome = await buy(route, req, signature, request)
        if (result.outcome.kind === 'bought') replay.set(key, result.outcome.bought)
      })
      if (!ran) {
        // Another request with the same signature was running; answer as it did.
        if (!answered()) res.status(409).json({ error: 'payment_in_use', reason: 'retry in a moment' })
        return
      }
      const outcome = result.outcome as Outcome
      switch (outcome.kind) {
        case 'bought':
          return send(res, outcome.bought, false)
        case 'bad_payment':
          res.status(400).json({ error: 'bad_payment', reason: outcome.reason })
          return
        case 'pay':
          return paymentRequired(route, req, res, outcome.reason)
        case 'failed':
          res.status(outcome.status).json(outcome.body)
      }
    }

    if (route.method === 'GET') router.get(route.path, handle)
    else router.post(route.path, handle)

    if (route.async) {
      router.get(`${route.path}/jobs/:jobId`, async (req: Request, res: Response) => {
        const jobId = String(req.params.jobId)
        const held = jobs.get(jobId)
        const given = (req.header('authorization') ?? '').replace(/^receipt\s+/i, '')
        if (!held || held.route !== route.id || !given || !sameSecret(held.receipt, sha256(given))) {
          res.status(404).json({ error: 'no_such_job' })
          return
        }
        if (Date.now() - held.at > jobTtl) {
          jobs.delete(jobId)
          res.status(410).json({ error: 'job_expired', charged: false })
          return
        }
        if (held.delivered) {
          res.status(410).json({ error: 'already_delivered' })
          return
        }
        let answer: { status: number; body: unknown; paymentResponse?: string } | null = null
        const polled = await replay.once(`job:${jobId}`, async () => {
          answer = await poll(route, jobId, held)
        })
        if (!polled || answer === null) {
          res.status(409).json({ error: 'poll_in_progress', reason: 'retry in a moment' })
          return
        }
        const { status, body, paymentResponse } = answer as {
          status: number
          body: unknown
          paymentResponse?: string
        }
        if (paymentResponse) res.setHeader('PAYMENT-RESPONSE', paymentResponse)
        res.status(status).json(body)
      })
    }
  }

  return router
}

/** The seller page's data: every paid route, its price, and how to pay (#205). */
export async function catalogOf(
  rail: Rail,
  routes: PaidRoute[],
  priced: Map<string, string>,
  extra: Record<string, unknown> = {},
): Promise<Record<string, unknown>> {
  let accepts: unknown[] = []
  try {
    accepts = (await rail.requirements('0.01')).map((r) => ({
      scheme: r.scheme,
      network: r.network,
      asset: r.asset,
      payTo: r.payTo,
    }))
  } catch {
    accepts = []
  }
  return {
    service: 'misthos',
    protocol: 'x402',
    x402Version: 2,
    rail: 'Circle Gateway Nanopayments (batched, gas-free for the buyer, EOA signatures only)',
    networks: rail.config.networks,
    payTo: rail.config.sellerAddress,
    accepts,
    ...extra,
    routes: routes.map((r) => ({
      id: r.id,
      method: r.method,
      path: r.path,
      price: { usdc: priced.get(r.id), base_units: baseUnits(priced.get(r.id) as string) },
      scheme: 'exact',
      description: r.description,
      free_tier: Boolean(r.freeTier),
      async: Boolean(r.async),
      capped: Boolean(r.capped),
      ...(r.note ? { note: r.note } : {}),
      ...(r.async ? { poll: `${r.path}/jobs/{job_id}` } : {}),
      ...(r.example ? { example: r.example } : {}),
    })),
  }
}

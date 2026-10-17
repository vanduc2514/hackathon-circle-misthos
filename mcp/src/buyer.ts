/**
 * The buyer's half of x402, for the Misthos paid API (#205).
 *
 * One call: ask; on 402 pick the Gateway batched option for our network, check the
 * price against the spend cap, sign an EIP-3009 authorisation with the agent's key,
 * and ask again with `PAYMENT-SIGNATURE`. The answer carries `PAYMENT-RESPONSE`, which
 * is the receipt. Nothing is signed that would take the session past its cap, and a
 * call the seller did not answer is not counted: the seller settles only after it
 * answers, so a failure costs nothing.
 *
 * Long work (the review) answers 202 with a job and a receipt. Its price is held
 * against the cap until the job is collected, then counted, or released if it failed.
 *
 * Errors come back as `{ error, detail }` in words a caller can act on, with any URL
 * or address of the seller's internals taken out.
 */

import { BatchEvmScheme } from '@circle-fin/x402-batching/client'
import type { Hex } from 'viem'
import { privateKeyToAccount } from 'viem/accounts'

export const ARC_TESTNET = 'eip155:5042002'

export interface BuyerConfig {
  /** The edge, such as https://paid.example.com. */
  baseUrl: string
  /** The agent wallet's key: an EOA, because Gateway takes no smart-account signature. */
  privateKey?: Hex
  network?: string
  /** The most this session may spend, in USDC. */
  spendCapUsdc: string
  /** The most one call may cost, in USDC. */
  callCapUsdc?: string
  /** A free-tier key, sent as `X-Api-Key`. */
  apiKey?: string
  /** A signed-in session, sent as a bearer token, for routes free to their owner. */
  session?: string
  fetch?: typeof fetch
}

export interface Paid {
  base_units: string
  usdc: string
  transaction: string
  network: string
  payer: string
}

export type Outcome =
  | { ok: true; status: number; data: unknown; paid?: Paid; held?: string; replayed?: boolean }
  | {
      ok: false
      status: number
      error: string
      detail?: string
      /** False when nothing was or could have been taken; 'unknown' when a signed
       * payment went out and no answer came back. */
      charged: false | 'unknown'
      /** The seller itself said nothing was charged. */
      uncharged?: boolean
    }

/** USDC decimal text to base units, refusing what is not an amount. */
export function units(usdc: string): bigint {
  if (!/^\d+(\.\d{1,6})?$/.test(usdc)) throw new Error(`not a USDC amount: ${usdc}`)
  const [whole, fraction = ''] = usdc.split('.')
  return BigInt(whole) * 1_000_000n + BigInt(fraction.padEnd(6, '0'))
}

export function usdc(baseUnits: bigint): string {
  const whole = baseUnits / 1_000_000n
  const fraction = (baseUnits % 1_000_000n).toString().padStart(6, '0').replace(/0+$/, '')
  return fraction ? `${whole}.${fraction.padEnd(2, '0')}` : `${whole}.00`
}

/** Strip anything that names where something lives: URLs, hosts with ports, IPs. */
export function clean(text: unknown): string {
  return String(text ?? '')
    .replace(/\b[a-z][a-z0-9+.-]*:\/\/\S+/gi, '[address]')
    .replace(/\b\d{1,3}(\.\d{1,3}){3}(:\d+)?\b/g, '[address]')
    .replace(/\blocalhost(:\d+)?\b/gi, '[address]')
    .slice(0, 500)
}

const decode = (header: string): Record<string, unknown> =>
  JSON.parse(Buffer.from(header, 'base64').toString('utf-8')) as Record<string, unknown>

interface Option {
  scheme: string
  network: string
  asset: string
  amount: string
  payTo: string
  maxTimeoutSeconds: number
  extra?: Record<string, unknown>
}

/**
 * What this session has spent, and what it holds.
 *
 * A price is held the moment it is about to be signed, so two calls at once cannot
 * both fit under the cap, and a signed authorisation is never forgotten. It is
 * released only when the seller says plainly that nothing was charged; anything less
 * certain is counted, because a cap that undercounts is not a cap.
 */
export class Spend {
  spent = 0n
  private readonly holds = new Map<string, bigint>()
  private readonly collected = new Map<string, bigint>()
  private next = 0

  constructor(
    readonly cap: bigint,
    readonly perCall: bigint,
  ) {}

  get held(): bigint {
    let total = 0n
    for (const amount of this.holds.values()) total += amount
    return total
  }

  /** Why `amount` may not be spent now, or null if it may. */
  refuse(amount: bigint): string | null {
    if (amount > this.perCall) {
      return `this call costs ${usdc(amount)} USDC, over the ${usdc(this.perCall)} USDC limit for one call`
    }
    if (this.spent + this.held + amount > this.cap) {
      return (
        `this call costs ${usdc(amount)} USDC and ${usdc(this.spent + this.held)} USDC is ` +
        `spent or held, so it would pass the ${usdc(this.cap)} USDC spend cap`
      )
    }
    return null
  }

  /** Hold `amount` before signing, or say why not. Returns the hold's key. */
  reserve(amount: bigint): { key: string } | { refused: string } {
    const refused = this.refuse(amount)
    if (refused) return { refused }
    const key = `call-${(this.next += 1)}`
    this.holds.set(key, amount)
    return { key }
  }

  /** Move a hold to the job it paid for, which is collected later. */
  rekey(from: string, job: string): void {
    const amount = this.holds.get(from)
    if (amount === undefined) return
    this.holds.delete(from)
    this.holds.set(job, amount)
  }

  heldFor(key: string): bigint {
    return this.holds.get(key) ?? this.collected.get(key) ?? 0n
  }

  /** Count a hold as spent. Counting the same job twice counts it once. */
  settle(key: string): bigint {
    const amount = this.holds.get(key)
    if (amount === undefined) return this.collected.get(key) ?? 0n
    this.holds.delete(key)
    this.spent += amount
    if (key.startsWith('call-') === false) this.collected.set(key, amount)
    return amount
  }

  /** Give a hold back: the seller said nothing was charged. */
  release(key: string): void {
    this.holds.delete(key)
  }

  summary() {
    const left = this.cap - this.spent - this.held
    return {
      spent: usdc(this.spent),
      held: usdc(this.held),
      cap: usdc(this.cap),
      per_call_cap: usdc(this.perCall),
      left: usdc(left > 0n ? left : 0n),
    }
  }
}

export class Buyer {
  readonly spend: Spend
  readonly network: string
  readonly address: string | null
  private readonly base: string
  private readonly scheme: BatchEvmScheme | null
  private readonly http: typeof fetch

  constructor(private readonly config: BuyerConfig) {
    this.base = config.baseUrl.replace(/\/+$/, '')
    this.network = config.network ?? ARC_TESTNET
    const cap = units(config.spendCapUsdc)
    this.spend = new Spend(cap, config.callCapUsdc ? units(config.callCapUsdc) : cap)
    this.http = config.fetch ?? fetch
    if (config.privateKey) {
      const account = privateKeyToAccount(config.privateKey)
      this.address = account.address
      this.scheme = new BatchEvmScheme({
        address: account.address,
        signTypedData: (params) => account.signTypedData(params as Parameters<typeof account.signTypedData>[0]),
      })
    } else {
      this.address = null
      this.scheme = null
    }
  }

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    return {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      ...(this.config.apiKey ? { 'X-Api-Key': this.config.apiKey } : {}),
      ...(this.config.session ? { Authorization: `Bearer ${this.config.session}` } : {}),
      ...extra,
    }
  }

  private async send(
    method: string,
    path: string,
    body: unknown,
    headers: Record<string, string>,
  ): Promise<Response | null> {
    try {
      return await this.http(`${this.base}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(120_000),
      })
    } catch {
      return null
    }
  }

  /** Call a paid route, paying for it if it asks, within the cap. */
  async call(method: 'GET' | 'POST', path: string, body?: unknown): Promise<Outcome> {
    const first = await this.send(method, path, body, this.headers())
    if (!first) return unreachable()
    if (first.status !== 402) return answer(first)

    const header = first.headers.get('payment-required')
    if (!header) return failure(502, 'bad_seller', 'a 402 with no PAYMENT-REQUIRED header')
    let required: Record<string, unknown>
    try {
      required = decode(header)
    } catch {
      return failure(502, 'bad_seller', 'PAYMENT-REQUIRED is not base64 JSON')
    }
    const accepts = (required.accepts ?? []) as Option[]
    const option = accepts.find(
      (o) =>
        o.scheme === 'exact' &&
        o.network === this.network &&
        o.extra?.name === 'GatewayWalletBatched' &&
        typeof o.extra?.verifyingContract === 'string',
    )
    if (!option) {
      return failure(402, 'no_supported_payment', `the seller takes no Gateway payment on ${this.network}`)
    }
    const amount = BigInt(option.amount)
    if (!this.scheme) {
      const refused = this.spend.refuse(amount)
      if (refused) return failure(402, 'spend_cap_exceeded', refused)
      return failure(402, 'no_wallet', 'set MISTHOS_BUYER_PRIVATE_KEY to pay; this route is not free to you')
    }
    const hold = this.spend.reserve(amount)
    if ('refused' in hold) return failure(402, 'spend_cap_exceeded', hold.refused)

    let signature: string
    try {
      const payload = await this.scheme.createPaymentPayload(Number(required.x402Version ?? 2), option)
      signature = Buffer.from(
        JSON.stringify({ ...payload, resource: required.resource, accepted: option }),
      ).toString('base64')
    } catch (err) {
      // Nothing was signed, so nothing can be charged.
      this.spend.release(hold.key)
      return failure(402, 'signing_failed', clean((err as Error).message))
    }

    const paid = await this.send(method, path, body, this.headers({ 'Payment-Signature': signature }))
    if (!paid) {
      // Signed and sent, and no answer: the seller may have settled before the answer
      // was lost. Counted, so the cap stays a cap.
      this.spend.settle(hold.key)
      return {
        ok: false,
        status: 503,
        error: 'no_answer',
        detail:
          'the paid API did not answer a signed payment; it is counted against the cap in ' +
          'case it settled, and the same call can be made again',
        charged: 'unknown',
      }
    }
    if (paid.status === 202) {
      const data = await json(paid)
      const job = String((data as { job_id?: unknown }).job_id ?? '')
      if (job) this.spend.rekey(hold.key, job)
      return { ok: true, status: 202, data, held: usdc(amount) }
    }
    const response = paid.headers.get('payment-response')
    const out = await answer(paid)
    if (!out.ok && out.uncharged) {
      this.spend.release(hold.key)
      return out
    }
    this.spend.settle(hold.key)
    if (out.ok && response) {
      return {
        ...out,
        paid: paidOf(response, amount, this.network),
        replayed: paid.headers.get('x-misthos-replayed') === 'true',
      }
    }
    return out
  }

  /** Collect a job. Free: the receipt is the way in. Counts its price once, when the
   * seller says it settled; gives it back only when the seller says the job failed. */
  async collect(path: string, job: string, receipt: string): Promise<Outcome> {
    const res = await this.send('GET', path, undefined, this.headers({ Authorization: `Receipt ${receipt}` }))
    if (!res) return unreachable()
    const out = await answer(res)
    const response = res.headers.get('payment-response')
    if (out.ok && res.status === 200 && response) {
      const amount = this.spend.settle(job)
      return {
        ...out,
        paid: paidOf(response, amount, this.network),
        replayed: res.headers.get('x-misthos-replayed') === 'true',
      }
    }
    const failed = out.ok && (out.data as { status?: unknown }).status === 'failed'
    if (failed) this.spend.release(job)
    // A delivered job (410) was settled: count it. A job the seller does not know (404)
    // is left held: the cap would rather overcount than undercount.
    if (!out.ok && out.status === 410) this.spend.settle(job)
    return out
  }
}

function paidOf(header: string, amount: bigint, network: string): Paid {
  let settled: Record<string, unknown> = {}
  try {
    settled = decode(header)
  } catch {
    settled = {}
  }
  return {
    base_units: amount.toString(),
    usdc: usdc(amount),
    transaction: String(settled.transaction ?? ''),
    network: String(settled.network ?? network),
    payer: String(settled.payer ?? ''),
  }
}

async function json(res: Response): Promise<unknown> {
  const text = await res.text()
  try {
    return text ? JSON.parse(text) : {}
  } catch {
    return { text: clean(text) }
  }
}

async function answer(res: Response): Promise<Outcome> {
  const data = await json(res)
  if (res.ok) return { ok: true, status: res.status, data }
  const body = (typeof data === 'object' && data !== null ? data : {}) as Record<string, unknown>
  const detail = body.detail ?? body.reason
  return {
    ok: false,
    status: res.status,
    error: String(body.error ?? `http_${res.status}`),
    ...(detail === undefined ? {} : { detail: clean(detail) }),
    charged: false,
    // A 402 is a refusal before anything was taken; otherwise the seller says so.
    uncharged: res.status === 402 || body.charged === false,
  }
}

function failure(status: number, error: string, detail: string): Outcome {
  return { ok: false, status, error, detail: clean(detail), charged: false }
}

function unreachable(): Outcome {
  return failure(503, 'seller_unreachable', 'the paid API did not answer; nothing was charged')
}

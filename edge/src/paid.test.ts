import assert from 'node:assert/strict'
import type { AddressInfo } from 'node:net'
import { describe, it } from 'node:test'
import express from 'express'
import { CATALOG } from './catalog.js'
import {
  CORE_PAID_PREFIX,
  ReplayCache,
  catalogOf,
  coreClient,
  freeApiKeys,
  insidePaidApi,
  paidRouter,
  prices,
  type Core,
  type PaidRoute,
  type SettlementLog,
} from './paid.js'
import { ARC_USDC, BUYER, LIVE, SELLER, fakeCore, fakeFacilitator, signed } from './test-fakes.js'
import { ARC_TESTNET, createRail } from './x402-gate.js'

const decode = (header: string | null) => JSON.parse(Buffer.from(header ?? '', 'base64').toString('utf-8'))
const FREE_KEY = 'free-key-0123456789abcdef'

const LOOKUP: PaidRoute = {
  id: 'lookup',
  method: 'GET',
  path: '/paid/lookup/:thing',
  price: '0.01',
  description: 'Look a thing up',
  name: 'Lookup',
  summary: 'Look a thing up.',
  group: 'Trust',
  core: (req) => ({
    method: 'GET',
    path: `${CORE_PAID_PREFIX}lookup/${encodeURIComponent(String(req.params.thing))}?page=${Number(req.query.page ?? 1)}`,
  }),
  freeTier: true,
}

const PRICED: PaidRoute = {
  id: 'priced',
  method: 'POST',
  path: '/paid/priced',
  price: '0.25',
  description: 'Price something',
  name: 'Price',
  summary: 'Price something.',
  group: 'Analytics',
  core: () => ({ method: 'POST', path: `${CORE_PAID_PREFIX}priced` }),
}

const REVIEW: PaidRoute = {
  id: 'review',
  method: 'POST',
  path: '/paid/review',
  price: '0.50',
  description: 'Review a diff',
  name: 'Review',
  summary: 'Review a diff.',
  group: 'Analytics',
  core: () => ({ method: 'POST', path: `${CORE_PAID_PREFIX}review` }),
  async: true,
  capped: true,
  note: 'the cap is the price',
}

interface Setup {
  core?: Core
  settleFails?: string
  unheard?: { times: number; taken: boolean }
  routes?: PaidRoute[]
  maxHeldPerPayer?: number
}

async function serve(setup: Setup = {}) {
  const { facilitator, calls } = fakeFacilitator({ settleFails: setup.settleFails, unheard: setup.unheard })
  const rail = createRail(LIVE, facilitator)
  const logged: Parameters<SettlementLog>[0][] = []
  const fallback = fakeCore()
  const app = express()
  app.use(express.json())
  const router = paidRouter({
    rail,
    routes: setup.routes ?? [LOOKUP, PRICED, REVIEW],
    core: setup.core ?? fallback.core,
    apiKeys: [FREE_KEY],
    replay: new ReplayCache(),
    log: (entry) => logged.push(entry),
    sweepMs: 0,
    maxHeldPerPayer: setup.maxHeldPerPayer,
  })
  app.use(router)
  const server = app.listen(0)
  await new Promise((resolve) => server.once('listening', resolve))
  const { port } = server.address() as AddressInfo
  const base = `http://127.0.0.1:${port}`
  return {
    calls,
    logged,
    router,
    hits: fallback.hits,
    get: (path: string, headers: Record<string, string> = {}) => fetch(base + path, { headers }),
    post: (path: string, body: unknown, headers: Record<string, string> = {}) =>
      fetch(base + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...headers },
        body: JSON.stringify(body),
      }),
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections()
        server.close(resolve)
      }),
  }
}

describe('an unpaid call', () => {
  it('answers 402 naming Arc testnet, its USDC, the seller and the amount', async () => {
    const app = await serve()
    const res = await app.get('/paid/lookup/abc?page=2')
    await app.close()

    assert.equal(res.status, 402)
    const required = decode(res.headers.get('payment-required'))
    assert.equal(required.x402Version, 2)
    assert.equal(required.resource.url, '/paid/lookup/abc?page=2')
    assert.equal(required.accepts.length, 1)
    const [accept] = required.accepts
    assert.equal(accept.scheme, 'exact')
    assert.equal(accept.network, 'eip155:5042002')
    assert.equal(accept.asset, ARC_USDC)
    assert.equal(accept.payTo, SELLER)
    assert.equal(accept.amount, '10000')
    assert.equal(accept.extra.name, 'GatewayWalletBatched')

    const body = (await res.json()) as Record<string, unknown>
    assert.equal(body.error, 'payment_required')
    assert.deepEqual(body.price, { usdc: '0.01', base_units: '10000' })
    assert.deepEqual(body.accepts, required.accepts)
    assert.equal(app.hits.length, 0, 'no work runs before a payment')
  })

  it('names the reason when the payment does not verify, and settles nothing', async () => {
    const app = await serve()
    const res = await app.get('/paid/lookup/abc', { 'PAYMENT-SIGNATURE': signed('10000', { signature: 'forged' }) })
    const body = (await res.json()) as Record<string, unknown>
    await app.close()
    assert.equal(res.status, 402)
    assert.equal(body.reason, 'invalid_signature')
    assert.equal(app.calls.settled.length, 0)
    assert.equal(app.hits.length, 0)
  })

  it('answers 400 to a payment header that is not a payment', async () => {
    const app = await serve()
    const res = await app.get('/paid/lookup/abc', { 'PAYMENT-SIGNATURE': 'not-base64-json' })
    await app.close()
    assert.equal(res.status, 400)
    assert.equal(((await res.json()) as { error: string }).error, 'bad_payment')
  })
})

describe('a paid call', () => {
  it('runs the work, settles, and answers with PAYMENT-RESPONSE', async () => {
    const { core, hits } = fakeCore(() => ({ status: 200, body: { answer: 42 } }))
    const app = await serve({ core })
    const res = await app.get('/paid/lookup/a%2Fb?page=3', { 'PAYMENT-SIGNATURE': signed('10000') })
    await app.close()

    assert.equal(res.status, 200)
    assert.deepEqual(await res.json(), { answer: 42 })
    assert.deepEqual(decode(res.headers.get('payment-response')), {
      success: true,
      transaction: '0xbatch-n-1',
      network: ARC_TESTNET,
      payer: BUYER,
    })
    assert.equal(hits.length, 1)
    assert.equal(hits[0].path, `${CORE_PAID_PREFIX}lookup/a%2Fb?page=3`)
    assert.equal(hits[0].headers['X-Misthos-Payer'], BUYER)
    assert.equal(hits[0].headers['X-Misthos-Paid-Route'], 'lookup')
    assert.equal(app.calls.settled.length, 1)
    assert.equal(app.logged.length, 1)
    assert.equal(app.logged[0].route, 'lookup')
    assert.equal(hits[0].headers['X-Misthos-Request-Digest'], app.logged[0].request, 'the core can join the payment')
    assert.deepEqual(app.logged[0].settlement, {
      payer: BUYER,
      network: ARC_TESTNET,
      amount: '10000',
      asset: ARC_USDC,
      transaction: '0xbatch-n-1',
      rail: 'gateway-nanopayments',
    })
  })

  it('answers the same signature again from the first answer, without running or settling twice', async () => {
    const { core, hits } = fakeCore(() => ({ status: 200, body: { n: hits.length } }))
    const app = await serve({ core })
    const pay = { 'PAYMENT-SIGNATURE': signed('10000', { nonce: 'n-dup' }) }
    const first = await app.get('/paid/lookup/x', pay)
    const again = await app.get('/paid/lookup/x', pay)
    await app.close()

    assert.equal(first.status, 200)
    assert.equal(again.status, 200)
    assert.deepEqual(await again.json(), await first.json())
    assert.equal(again.headers.get('x-misthos-replayed'), 'true')
    assert.equal(again.headers.get('payment-response'), first.headers.get('payment-response'))
    assert.equal(hits.length, 1)
    assert.equal(app.calls.settled.length, 1)
  })

  it('runs and settles once when the same signature arrives twice at the same moment', async () => {
    const { core, hits } = fakeCore(() => ({ status: 200, body: { ok: true } }))
    const app = await serve({ core })
    const pay = { 'PAYMENT-SIGNATURE': signed('10000', { nonce: 'n-race' }) }
    const both = await Promise.all([app.get('/paid/lookup/x', pay), app.get('/paid/lookup/x', pay)])
    await app.close()
    assert.deepEqual(both.map((r) => r.status), [200, 200])
    assert.equal(hits.length, 1)
    assert.equal(app.calls.settled.length, 1)
  })

  it('refuses a signature that already bought a different request', async () => {
    const app = await serve()
    const pay = { 'PAYMENT-SIGNATURE': signed('10000', { nonce: 'n-reuse' }) }
    assert.equal((await app.get('/paid/lookup/one', pay)).status, 200)
    const other = await app.get('/paid/lookup/two', pay)
    await app.close()
    assert.equal(other.status, 409)
    assert.equal(((await other.json()) as { error: string }).error, 'payment_already_used')
    assert.equal(app.calls.settled.length, 1)
  })

  it('knows a payment by its authorisation, however the header is spelled', async () => {
    const { core, hits } = fakeCore(() => ({ status: 200, body: { ok: true } }))
    const app = await serve({ core })
    const first = await app.get('/paid/lookup/x', { 'PAYMENT-SIGNATURE': signed('10000', { nonce: 'n-enc' }) })
    const respelled = await app.get('/paid/lookup/x', {
      'PAYMENT-SIGNATURE': signed('10000', { nonce: 'n-enc', indent: 2 }),
    })
    const elsewhere = await app.get('/paid/lookup/y', {
      'PAYMENT-SIGNATURE': signed('10000', { nonce: 'n-enc', indent: 4 }),
    })
    await app.close()
    assert.equal(first.status, 200)
    assert.equal(respelled.headers.get('x-misthos-replayed'), 'true')
    assert.equal(elsewhere.status, 409)
    assert.equal(hits.length, 1, 'the work ran once')
    assert.equal(app.calls.settled.length, 1)
  })

  it('keeps the answer when a settlement goes unheard, and delivers it once Gateway says it was taken', async () => {
    const { core, hits } = fakeCore(() => ({ status: 200, body: { secret: 'the paid result' } }))
    const app = await serve({ core, unheard: { times: 1, taken: true } })
    const pay = { 'PAYMENT-SIGNATURE': signed('10000', { nonce: 'n-lost' }) }
    const unheard = await app.get('/paid/lookup/x', pay)
    const text = await unheard.text()
    assert.equal(unheard.status, 502)
    assert.equal(JSON.parse(text).error, 'settlement_unconfirmed')
    assert.equal(JSON.parse(text).charged, 'unknown')
    assert.doesNotMatch(text, /the paid result/)

    const retried = await app.get('/paid/lookup/x', pay)
    await app.close()
    assert.equal(retried.status, 200)
    assert.deepEqual(await retried.json(), { secret: 'the paid result' })
    assert.equal(hits.length, 1, 'the work was not run twice')
  })

  it('settles on the retry when the first settlement was never taken', async () => {
    const { core, hits } = fakeCore(() => ({ status: 200, body: { ok: true } }))
    const app = await serve({ core, unheard: { times: 1, taken: false } })
    const pay = { 'PAYMENT-SIGNATURE': signed('10000', { nonce: 'n-retry-settle' }) }
    assert.equal((await app.get('/paid/lookup/x', pay)).status, 502)
    const retried = await app.get('/paid/lookup/x', pay)
    await app.close()
    assert.equal(retried.status, 200)
    assert.ok(retried.headers.get('payment-response'))
    assert.equal(hits.length, 1)
    assert.equal(app.logged.length, 1)
  })

  it('withholds the result when settlement fails', async () => {
    const { core } = fakeCore(() => ({ status: 200, body: { secret: 'the paid result' } }))
    const app = await serve({ core, settleFails: 'insufficient_balance' })
    const res = await app.get('/paid/lookup/x', { 'PAYMENT-SIGNATURE': signed('10000') })
    const text = await res.text()
    await app.close()
    assert.equal(res.status, 402)
    assert.deepEqual(JSON.parse(text), { error: 'settlement_failed', reason: 'insufficient_balance', charged: false })
    assert.doesNotMatch(text, /the paid result/)
    assert.equal(res.headers.get('payment-response'), null)
    assert.equal(app.logged.length, 0)
  })

  it('takes nothing when the work fails, so the same signature can be used again', async () => {
    let down = true
    const { core, hits } = fakeCore(() =>
      down ? { status: 503, body: { error: 'busy' } } : { status: 200, body: { ok: true } },
    )
    const app = await serve({ core })
    const pay = { 'PAYMENT-SIGNATURE': signed('10000', { nonce: 'n-retry' }) }
    const failed = await app.get('/paid/lookup/x', pay)
    assert.equal(failed.status, 503)
    assert.deepEqual(await failed.json(), { error: 'busy', charged: false })
    assert.equal(app.calls.settled.length, 0)

    down = false
    const retried = await app.get('/paid/lookup/x', pay)
    await app.close()
    assert.equal(retried.status, 200)
    assert.equal(hits.length, 2)
    assert.equal(app.calls.settled.length, 1)
  })

  it('charges what the route costs, so a payment signed for less is refused', async () => {
    const app = await serve()
    const res = await app.post('/paid/priced', {}, { 'PAYMENT-SIGNATURE': signed('10000') })
    await app.close()
    assert.equal(res.status, 402)
    assert.equal(((await res.json()) as { reason: string }).reason, 'insufficient_amount')
    assert.equal(app.calls.verified[0].amount, '250000')
  })
})

describe('the free tier', () => {
  it('serves a free API key without a payment', async () => {
    const { core, hits } = fakeCore(() => ({ status: 200, body: { ok: true } }))
    const app = await serve({ core })
    const res = await app.get('/paid/lookup/x', { 'X-Api-Key': FREE_KEY })
    await app.close()
    assert.equal(res.status, 200)
    assert.equal(hits.length, 1)
    assert.equal(hits[0].headers['X-Misthos-Free-Caller'], 'api-key')
    assert.equal(app.calls.verified.length + app.calls.settled.length, 0)
  })

  it('serves a session the core says may use the route free, and asks it which route', async () => {
    const { core, hits } = fakeCore((hit) =>
      hit.path.endsWith('/access') ? { status: 200, body: { free: true, who: 'publisher:pub-1' } } : { status: 200, body: { ok: true } },
    )
    const app = await serve({ core })
    const res = await app.get('/paid/lookup/x?page=2', { Authorization: 'Bearer session-token' })
    await app.close()
    assert.equal(res.status, 200)
    assert.equal(hits[0].path, `${CORE_PAID_PREFIX}access`)
    assert.equal(hits[0].headers['X-Misthos-Caller-Authorization'], 'Bearer session-token')
    assert.deepEqual(hits[0].body, { route: 'lookup', params: { thing: 'x' }, query: { page: '2' } })
    assert.equal(hits[1].headers['X-Misthos-Free-Caller'], 'publisher:pub-1')
  })

  it('asks a session the core does not recognise to pay', async () => {
    const { core } = fakeCore(() => ({ status: 200, body: { free: false } }))
    const app = await serve({ core })
    const res = await app.get('/paid/lookup/x', { Authorization: 'Bearer stranger' })
    await app.close()
    assert.equal(res.status, 402)
  })

  it('is not offered on a route without one, whatever key the caller holds', async () => {
    const app = await serve()
    const res = await app.post('/paid/priced', {}, { 'X-Api-Key': FREE_KEY })
    await app.close()
    assert.equal(res.status, 402)
    assert.equal(app.hits.length, 0)
  })
})

describe('an async paid call', () => {
  it('names the exact scheme and the cap when unpaid, since Gateway has no upto', async () => {
    const app = await serve()
    const res = await app.post('/paid/review', { diff: '+x' })
    const body = (await res.json()) as Record<string, unknown>
    await app.close()
    assert.equal(res.status, 402)
    assert.equal(body.scheme, 'exact')
    assert.deepEqual(body.cap, { usdc: '0.50', base_units: '500000' })
    assert.equal(body.upto, false)
    assert.equal(body.async, true)
    assert.equal(body.note, 'the cap is the price')
    const required = decode(res.headers.get('payment-required'))
    assert.equal(required.accepts[0].scheme, 'exact')
    assert.equal(required.accepts[0].amount, '500000')
  })

  function reviewCore() {
    let state = 'queued'
    const fake = fakeCore((hit) => {
      if (hit.path === `${CORE_PAID_PREFIX}review`) return { status: 202, body: { job_id: 'job-1', status: 'queued' } }
      if (hit.path === `${CORE_PAID_PREFIX}review/jobs/job-1`) {
        return { status: 200, body: state === 'done' ? { status: 'done', verdict: 'approve' } : { status: state } }
      }
      return { status: 404, body: { error: 'nope' } }
    })
    return { ...fake, set: (s: string) => (state = s) }
  }

  it('answers 202 with a job and a receipt, and settles once when the result is delivered', async () => {
    const fake = reviewCore()
    const app = await serve({ core: fake.core })
    const queued = await app.post('/paid/review', { diff: '+x' }, { 'PAYMENT-SIGNATURE': signed('500000') })
    assert.equal(queued.status, 202)
    const job = (await queued.json()) as { job_id: string; receipt: string; poll: string; charged: boolean }
    assert.equal(job.job_id, 'job-1')
    assert.equal(job.poll, '/paid/review/jobs/job-1')
    assert.equal(job.charged, false)
    assert.equal(app.calls.settled.length, 0)

    const receipt = { Authorization: `Receipt ${job.receipt}` }
    assert.equal((await app.get(job.poll)).status, 404, 'the receipt is the only way in')
    assert.equal((await app.get(job.poll, { Authorization: 'Receipt wrong' })).status, 404)
    assert.equal((await app.get(job.poll, receipt)).status, 202)
    assert.equal(app.calls.settled.length, 0)

    fake.set('done')
    const done = await app.get(job.poll, receipt)
    assert.equal(done.status, 200)
    assert.deepEqual(await done.json(), { status: 'done', verdict: 'approve' })
    assert.equal(decode(done.headers.get('payment-response')).payer, BUYER)
    assert.equal(app.calls.settled.length, 1)
    assert.equal(app.logged[0].ref, 'job-1')

    const again = await app.get(job.poll, receipt)
    await app.close()
    assert.equal(again.status, 200, 'a lost answer is not a lost purchase')
    assert.equal(again.headers.get('x-misthos-replayed'), 'true')
    assert.deepEqual(await again.json(), { status: 'done', verdict: 'approve' })
    assert.equal(app.calls.settled.length, 1, 'settled once, however often it is read')
  })

  it('settles a finished review when it is done, whether or not its buyer polls', async () => {
    const fake = reviewCore()
    const app = await serve({ core: fake.core })
    const queued = await app.post('/paid/review', { diff: '+x' }, { 'PAYMENT-SIGNATURE': signed('500000') })
    const job = (await queued.json()) as { receipt: string; poll: string }
    await app.router.sweep()
    assert.equal(app.calls.settled.length, 0, 'not done, not settled')
    fake.set('done')
    await app.router.sweep()
    await app.router.sweep()
    assert.equal(app.calls.settled.length, 1, 'done, settled once')
    assert.equal(app.logged.length, 1)
    const collected = await app.get(job.poll, { Authorization: `Receipt ${job.receipt}` })
    await app.close()
    assert.equal(collected.status, 200)
    assert.ok(collected.headers.get('payment-response'))
    assert.equal(app.calls.settled.length, 1)
  })

  it('holds only a few unsettled jobs for one wallet', async () => {
    let next = 0
    const fake = fakeCore((hit) =>
      hit.path === `${CORE_PAID_PREFIX}review`
        ? { status: 202, body: { job_id: `job-${(next += 1)}`, status: 'queued' } }
        : { status: 200, body: { status: 'running' } },
    )
    const app = await serve({ core: fake.core, maxHeldPerPayer: 2 })
    const first = await app.post('/paid/review', { a: 1 }, { 'PAYMENT-SIGNATURE': signed('500000', { nonce: 'j-1' }) })
    const second = await app.post('/paid/review', { a: 2 }, { 'PAYMENT-SIGNATURE': signed('500000', { nonce: 'j-2' }) })
    const third = await app.post('/paid/review', { a: 3 }, { 'PAYMENT-SIGNATURE': signed('500000', { nonce: 'j-3' }) })
    await app.close()
    assert.deepEqual([first.status, second.status, third.status], [202, 202, 429])
    assert.equal(((await third.json()) as { error: string }).error, 'too_many_jobs')
    assert.equal(fake.hits.filter((h) => h.path === `${CORE_PAID_PREFIX}review`).length, 2, 'the third ran nothing')
  })

  it('keeps a job through a core that cannot answer, and settles it when it can', async () => {
    let down = false
    const fake = reviewCore()
    const flaky: Core = {
      call: (call, opts) =>
        down && call.path.includes('/jobs/') ? Promise.resolve({ status: 503, body: { error: 'busy' } }) : fake.core.call(call, opts),
    }
    const app = await serve({ core: flaky })
    const queued = await app.post('/paid/review', { diff: '+x' }, { 'PAYMENT-SIGNATURE': signed('500000') })
    const job = (await queued.json()) as { receipt: string; poll: string }
    const receipt = { Authorization: `Receipt ${job.receipt}` }
    fake.set('done')
    down = true
    const outage = await app.get(job.poll, receipt)
    assert.equal(outage.status, 503)
    down = false
    const done = await app.get(job.poll, receipt)
    await app.close()
    assert.equal(done.status, 200)
    assert.equal(app.calls.settled.length, 1)
  })

  it('never charges a job that failed', async () => {
    const fake = reviewCore()
    const app = await serve({ core: fake.core })
    const queued = await app.post('/paid/review', { diff: '+x' }, { 'PAYMENT-SIGNATURE': signed('500000') })
    const job = (await queued.json()) as { receipt: string; poll: string }
    fake.set('failed')
    const failed = await app.get(job.poll, { Authorization: `Receipt ${job.receipt}` })
    const gone = await app.get(job.poll, { Authorization: `Receipt ${job.receipt}` })
    await app.close()
    assert.equal(failed.status, 200)
    assert.deepEqual(await failed.json(), { status: 'failed', charged: false })
    assert.equal(gone.status, 404)
    assert.equal(app.calls.settled.length, 0)
  })
})

describe('a route that is switched off', () => {
  it('answers 503 before naming a price, so nobody signs a payment for it', async () => {
    const off: PaidRoute = { ...PRICED, id: 'off', path: '/paid/off', unavailable: () => 'not yet' }
    const app = await serve({ routes: [off] })
    const res = await app.post('/paid/off', {}, { 'PAYMENT-SIGNATURE': signed('250000') })
    const body = await res.json()
    await app.close()
    assert.equal(res.status, 503)
    assert.deepEqual(body, { error: 'route_unavailable', route: 'off', reason: 'not yet', charged: false })
    assert.equal(res.headers.get('payment-required'), null)
    assert.equal(app.calls.verified.length + app.calls.settled.length + app.hits.length, 0)
  })

  it('keeps the sanctions screen off unless EDGE_PAID_SCREENING is true', () => {
    const screen = CATALOG.find((r) => r.id === 'screen') as PaidRoute
    const before = process.env.EDGE_PAID_SCREENING
    delete process.env.EDGE_PAID_SCREENING
    assert.match(String(screen.unavailable?.()), /legal review/)
    process.env.EDGE_PAID_SCREENING = 'true'
    assert.equal(screen.unavailable?.(), null)
    if (before === undefined) delete process.env.EDGE_PAID_SCREENING
    else process.env.EDGE_PAID_SCREENING = before
  })
})

describe('the allowlist', () => {
  it('serves only what the catalog lists', async () => {
    const app = await serve({ routes: [PRICED] })
    const res = await app.get('/paid/lookup/x', { 'PAYMENT-SIGNATURE': signed('10000') })
    await app.close()
    assert.equal(res.status, 404)
    assert.equal(app.hits.length, 0)
  })

  it('refuses a route outside /paid/', () => {
    const { facilitator } = fakeFacilitator()
    const stray: PaidRoute = { ...PRICED, id: 'stray', path: '/api/v1/auth/me' }
    assert.throws(
      () => paidRouter({ rail: createRail(LIVE, facilitator), routes: [stray], core: fakeCore().core }),
      /must live under \/paid\//,
    )
  })

  it('never forwards outside the core paid API, whatever the route builds', async () => {
    const escape: PaidRoute = {
      ...LOOKUP,
      id: 'escape',
      path: '/paid/escape/:to',
      freeTier: false,
      core: (req) => ({ method: 'GET', path: `${CORE_PAID_PREFIX}${String(req.params.to)}` }),
    }
    const app = await serve({ routes: [escape] })
    const res = await app.get('/paid/escape/..%2F..%2Fauth%2Fme', { 'PAYMENT-SIGNATURE': signed('10000') })
    await app.close()
    assert.equal(res.status, 500)
    assert.equal(app.hits.length, 0)
    assert.equal(app.calls.settled.length, 0)
  })

  it('keeps every paid path inside the paid API', () => {
    for (const ok of [`${CORE_PAID_PREFIX}decisions?page=1`, `${CORE_PAID_PREFIX}reputation/c%2D1`]) {
      assert.ok(insidePaidApi(ok), ok)
    }
    for (const bad of [
      '/api/v1/auth/me',
      `${CORE_PAID_PREFIX}../auth/me`,
      `${CORE_PAID_PREFIX}..%2F..%2Fauth`,
      `${CORE_PAID_PREFIX}%2e%2e/auth`,
      `${CORE_PAID_PREFIX}x/%E0%A4%A`,
    ]) {
      assert.ok(!insidePaidApi(bad), bad)
    }
  })

  it('lists every catalog route under /paid/, priced, with its core call inside the paid API', () => {
    const priced = prices(CATALOG, {})
    for (const route of CATALOG) {
      assert.match(route.path, /^\/paid\//, route.id)
      assert.ok(priced.get(route.id), route.id)
      const named = [...route.path.matchAll(/:(\w+)/g)].map(([, name]) => name)
      const params = { ...Object.fromEntries(named.map((n) => [n, 'x1'])), ...route.example?.params }
      for (const name of named) assert.ok(route.example?.params?.[name], `${route.id} shows ${name}`)
      const query = route.example?.query ?? {}
      const search = new URLSearchParams(query).toString()
      const req = { params, query, body: route.example?.body ?? {}, originalUrl: `${route.path}${search ? `?${search}` : ''}` }
      const call = route.core(req as never)
      assert.ok(insidePaidApi(call.path), `${route.id} calls ${call.path}`)
      assert.doesNotMatch(call.path, /\/(auth|wallets?|statements?|policy|lifecycle)\b/, route.id)
      assert.equal(call.path.includes('?'), search !== '', `${route.id} forwards the query as sent`)
      assert.ok(route.name && route.summary.length <= 100, `${route.id} has a name and a one-line summary`)
      assert.ok(['Core', 'Marketplace', 'Analytics', 'Trust'].includes(route.group), route.id)
    }
    assert.equal(new Set(CATALOG.map((r) => r.id)).size, CATALOG.length, 'route ids are unique')
  })

  it('sells every route at a price inside the range #200 set', () => {
    const ranges: Record<string, [number, number]> = {
      decisions: [0.01, 0.05],
      reputation: [0.02, 0.1],
      'escrow-attestation': [0.01, 0.05],
      price: [0.1, 0.5],
      comparables: [0.05, 0.25],
      review: [1.0, 6.31],
      opportunities: [0.01, 0.05],
      receipt: [0.01, 0.01],
      screen: [0.1, 0.5],
    }
    for (const [id, [low, high]] of Object.entries(ranges)) {
      const route = CATALOG.find((r) => r.id === id)
      assert.ok(route, id)
      assert.ok(Number(route.price) >= low && Number(route.price) <= high, `${id} at ${route.price}`)
    }
  })

  it('keeps a route parameter inside its own segment', () => {
    const attestation = CATALOG.find((r) => r.id === 'escrow-attestation') as PaidRoute
    const req = { params: { issue_id: '../../auth/me' }, query: {}, originalUrl: '/paid/escrow/x/attestation' }
    const call = attestation.core(req as never)
    assert.equal(call.path, `${CORE_PAID_PREFIX}escrow/..%2F..%2Fauth%2Fme/attestation`)
    assert.equal(insidePaidApi(call.path), false, 'so the router refuses it and charges nothing')
  })
})

describe('configuration', () => {
  it('takes per-route prices from EDGE_PRICES and refuses anything it cannot charge', () => {
    const routes = [LOOKUP, PRICED]
    assert.equal(prices(routes, {}).get('priced'), '0.25')
    assert.equal(prices(routes, { EDGE_PRICES: '{"priced":"0.40"}' }).get('priced'), '0.40')
    assert.throws(() => prices(routes, { EDGE_PRICES: '{"nope":"0.40"}' }), /not a paid route/)
    assert.throws(() => prices(routes, { EDGE_PRICES: '{"priced":"0"}' }), /more than zero/)
    assert.throws(() => prices(routes, { EDGE_PRICES: '{"priced":0.4}' }), /must be a string/)
    assert.throws(() => prices(routes, { EDGE_PRICES: '[1]' }), /JSON object/)
  })

  it('takes free API keys of at least 16 characters', () => {
    assert.deepEqual(freeApiKeys({ EDGE_FREE_API_KEYS: ` ${FREE_KEY}, short ,,` }), [FREE_KEY])
    assert.deepEqual(freeApiKeys({}), [])
  })

  it('takes an answer from the core that is not JSON as no answer at all', async () => {
    const { createServer } = await import('node:http')
    const proxy = createServer((req, res) => {
      res.writeHead(200, { 'Content-Type': 'text/html' })
      res.end(req.url?.endsWith('/empty') ? '' : '<html>maintenance</html>')
    }).listen(0)
    await new Promise((resolve) => proxy.once('listening', resolve))
    const client = coreClient(`http://127.0.0.1:${(proxy.address() as AddressInfo).port}`, 'token')
    const html = await client.call({ method: 'GET', path: `${CORE_PAID_PREFIX}x` })
    const empty = await client.call({ method: 'GET', path: `${CORE_PAID_PREFIX}empty` })
    proxy.closeAllConnections()
    await new Promise((resolve) => proxy.close(resolve))
    assert.deepEqual(html, { status: 502, body: { error: 'core_answered_non_json' } })
    assert.equal(empty.status, 502, 'so nothing is settled for it')
  })

  it('never tells a buyer where the core is', async () => {
    const res = await coreClient('http://10.0.0.9:1', 'token', 2000).call({ method: 'GET', path: `${CORE_PAID_PREFIX}x` })
    assert.equal(res.status, 502)
    assert.deepEqual(res.body, { error: 'core_unavailable' })
  })

  it('publishes a catalog of routes, prices and how to pay', async () => {
    const { facilitator } = fakeFacilitator()
    const rail = createRail(LIVE, facilitator)
    const catalog = (await catalogOf(rail, [LOOKUP, REVIEW], prices([LOOKUP, REVIEW], {}))) as {
      payTo: string
      accepts: { network: string; asset: string }[]
      routes: { id: string; price: { usdc: string; base_units: string }; free_tier: boolean; poll?: string }[]
    }
    assert.equal(catalog.payTo, SELLER)
    assert.deepEqual(catalog.accepts[0], { scheme: 'exact', network: ARC_TESTNET, asset: ARC_USDC, payTo: SELLER })
    assert.deepEqual(catalog.routes[0].price, { usdc: '0.01', base_units: '10000' })
    assert.equal(catalog.routes[0].free_tier, true)
    assert.equal(catalog.routes[1].poll, '/paid/review/jobs/{job_id}')
    assert.deepEqual(
      (catalog.routes as unknown as { name: string; group: string }[]).map((r) => [r.name, r.group]),
      [['Lookup', 'Trust'], ['Review', 'Analytics']],
    )
  })
})

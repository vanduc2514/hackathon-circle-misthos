import assert from 'node:assert/strict'
import { after, before, describe, it } from 'node:test'
import { privateKeyToAccount } from 'viem/accounts'
import { Buyer, clean, units, usdc } from './buyer.js'
import { STANDARD, fakeSeller, type FakeSeller } from './fake-seller.js'
import { configFrom } from './index.js'

const KEY = `0x${'59'.repeat(32)}` as const
const ADDRESS = privateKeyToAccount(KEY).address

describe('a buyer', () => {
  let seller: FakeSeller
  before(async () => {
    seller = await fakeSeller(STANDARD)
  })
  after(() => seller.close())

  const buyer = (overrides: Partial<ConstructorParameters<typeof Buyer>[0]> = {}) =>
    new Buyer({ baseUrl: seller.url, privateKey: KEY, spendCapUsdc: '1.00', ...overrides })

  it('pays a 402 with the agent wallet and returns the answer and the receipt', async () => {
    const b = buyer()
    const out = await b.call('GET', '/paid/reputation/CON-1')
    assert.ok(out.ok, JSON.stringify(out))
    assert.deepEqual(out.data, { contributor_id: 'CON-1', score: 42 })
    assert.equal(out.paid?.usdc, '0.05')
    assert.equal(out.paid?.base_units, '50000')
    assert.match(out.paid?.transaction ?? '', /^batch-/)
    assert.equal(out.paid?.payer, ADDRESS)
    assert.equal(b.spend.summary().spent, '0.05')
    const last = seller.calls.at(-1)
    assert.equal(last?.paid, true)
    assert.equal(last?.payer, ADDRESS, 'the seller recovered our wallet from the signature')
  })

  it('posts a body and pays for it', async () => {
    const out = await buyer().call('POST', '/paid/price', { title: 'x', repo: 'a/b' })
    assert.ok(out.ok)
    assert.deepEqual((out.data as { asked: unknown }).asked, { title: 'x', repo: 'a/b' })
  })

  it('signs nothing that would pass the spend cap', async () => {
    const b = buyer({ spendCapUsdc: '0.30' })
    assert.ok((await b.call('POST', '/paid/price', {})).ok)
    const before = seller.calls.length
    const refused = await b.call('GET', '/paid/reputation/CON-1')
    assert.equal(refused.ok, true, 'a 0.05 call still fits under 0.30')
    const over = await b.call('POST', '/paid/price', {})
    assert.equal(over.ok, false)
    assert.equal(!over.ok && over.error, 'spend_cap_exceeded')
    assert.match(!over.ok ? (over.detail ?? '') : '', /0\.25 USDC.*0\.30 USDC spend cap/)
    assert.equal(seller.calls.slice(before).filter((c) => c.paid).length, 1, 'only the call that fit was paid')
    assert.equal(b.spend.summary().spent, '0.30')
  })

  it('holds each price before signing, so calls at once cannot pass the cap together', async () => {
    const b = buyer({ spendCapUsdc: '0.50' })
    const before = seller.calls.filter((c) => c.paid).length
    const all = await Promise.all(Array.from({ length: 5 }, () => b.call('POST', '/paid/price', {})))
    const paid = all.filter((o) => o.ok).length
    assert.equal(paid, 2, 'two 0.25 calls fit under 0.50')
    assert.equal(all.filter((o) => !o.ok && o.error === 'spend_cap_exceeded').length, 3)
    assert.equal(seller.calls.filter((c) => c.paid).length - before, 2, 'only two were signed')
    assert.equal(b.spend.summary().spent, '0.50')
  })

  it('counts a signed payment that got no answer, and gives back one the seller refused', async () => {
    let calls = 0
    const flaky: typeof fetch = async (input, init) => {
      calls += 1
      // The first ask is answered (402); the paid ask is lost on the way back.
      if (calls === 2) throw new Error('socket hang up')
      return fetch(input, init)
    }
    const b = buyer({ fetch: flaky })
    const lost = await b.call('GET', '/paid/reputation/CON-1')
    assert.equal(!lost.ok && lost.error, 'no_answer')
    assert.equal(!lost.ok && lost.charged, 'unknown')
    assert.equal(b.spend.summary().spent, '0.05')

    const refused = await b.call('GET', '/paid/escrow/ISS-1/attestation')
    assert.equal(!refused.ok && refused.error, 'chain_unavailable')
    assert.equal(b.spend.summary().spent, '0.05', 'the seller said charged: false, so it was given back')
    assert.equal(b.spend.summary().held, '0.00')
  })

  it('signs nothing over the limit for one call', async () => {
    const out = await buyer({ spendCapUsdc: '10', callCapUsdc: '1.00' }).call('POST', '/paid/review', { criteria: ['x'], diff: 'd' })
    assert.equal(!out.ok && out.error, 'spend_cap_exceeded')
    assert.match(!out.ok ? (out.detail ?? '') : '', /over the 1\.00 USDC limit for one call/)
  })

  it('counts nothing when the seller fails, and repeats no internal address', async () => {
    const b = buyer()
    const out = await b.call('GET', '/paid/escrow/ISS-1/attestation')
    assert.equal(out.ok, false)
    assert.equal(!out.ok && out.error, 'chain_unavailable')
    assert.equal(!out.ok && out.charged, false)
    assert.doesNotMatch(JSON.stringify(out), /10\.1\.2\.3|http:/)
    assert.equal(b.spend.summary().spent, '0.00')
  })

  it('says it cannot pay without a wallet, and signs nothing', async () => {
    const out = await buyer({ privateKey: undefined }).call('GET', '/paid/reputation/CON-1')
    assert.equal(!out.ok && out.error, 'no_wallet')
  })

  it('takes a free answer without paying', async () => {
    const b = buyer({ privateKey: undefined, apiKey: 'free-key-0123456789' })
    const out = await b.call('GET', '/paid/reputation/CON-2')
    assert.ok(out.ok)
    assert.equal(out.paid, undefined)
    assert.equal(b.spend.summary().spent, '0.00')
  })

  it('holds a review against the cap until it is collected, then counts it once', async () => {
    const b = buyer({ spendCapUsdc: '3.00' })
    const queued = await b.call('POST', '/paid/review', { criteria: ['x'], diff: 'd' })
    assert.ok(queued.ok && queued.status === 202)
    const job = queued.data as { job_id: string; receipt: string; poll: string }
    assert.deepEqual(b.spend.summary(), { spent: '0.00', held: '2.00', cap: '3.00', per_call_cap: '3.00', left: '1.00' })

    const running = await b.collect(job.poll, job.job_id, job.receipt)
    assert.ok(running.ok && running.status === 202)
    const done = await b.collect(job.poll, job.job_id, job.receipt)
    assert.ok(done.ok && done.status === 200)
    assert.equal(done.paid?.usdc, '2.00')
    assert.equal(b.spend.summary().spent, '2.00')
    assert.equal(b.spend.summary().held, '0.00')

    const again = await b.collect(job.poll, job.job_id, job.receipt)
    assert.equal(!again.ok && again.status, 410)
    assert.equal(b.spend.summary().spent, '2.00', 'a delivered review is counted once')
  })

  it('releases the hold on a review that failed', async () => {
    const b = buyer({ spendCapUsdc: '3.00' })
    const queued = await b.call('POST', '/paid/review', { criteria: ['x'], diff: 'd', fail: true })
    const job = queued.ok ? (queued.data as { job_id: string; receipt: string; poll: string }) : null
    assert.ok(job)
    const failed = await b.collect(job.poll, job.job_id, job.receipt)
    assert.ok(failed.ok)
    assert.equal((failed.data as { status: string }).status, 'failed')
    assert.deepEqual([b.spend.summary().spent, b.spend.summary().held], ['0.00', '0.00'])
  })

  it('answers plainly when the seller is not there', async () => {
    const out = await new Buyer({ baseUrl: 'http://127.0.0.1:9', privateKey: KEY, spendCapUsdc: '1' }).call('GET', '/paid/x')
    assert.equal(!out.ok && out.error, 'seller_unreachable')
    assert.doesNotMatch(JSON.stringify(out), /127\.0\.0\.1/)
  })
})

describe('the money and the words', () => {
  it('reads and writes USDC in base units without floating point', () => {
    assert.equal(units('0.05'), 50_000n)
    assert.equal(units('2'), 2_000_000n)
    assert.equal(usdc(50_000n), '0.05')
    assert.equal(usdc(2_000_000n), '2.00')
    assert.equal(usdc(1n), '0.000001')
    assert.throws(() => units('1e3'), /not a USDC amount/)
  })

  it('takes addresses out of what it repeats', () => {
    assert.equal(clean('failed at https://core.internal:8000/api'), 'failed at [address]')
    assert.equal(clean('rpc 10.0.0.5:8545 down'), 'rpc [address] down')
    assert.equal(clean('localhost:8000 refused'), '[address] refused')
  })

  it('refuses a key that is not one', () => {
    assert.throws(() => configFrom({ MISTHOS_BUYER_PRIVATE_KEY: 'nope' }), /32-byte hex key/)
    assert.equal(configFrom({}).privateKey, undefined)
    assert.equal(configFrom({}).spendCapUsdc, '5.00')
    assert.equal(configFrom({}).network, 'eip155:5042002')
  })
})

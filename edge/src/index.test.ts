import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { fileURLToPath } from 'node:url'
import { after, before, describe, it } from 'node:test'
import { ARC_USDC, GATEWAY_WALLET } from './test-fakes.js'

const ENTRY = fileURLToPath(new URL('./index.ts', import.meta.url))
const EDGE = fileURLToPath(new URL('..', import.meta.url))
const SELLER = '0x1111111111111111111111111111111111111111'
const CIRCLE = { CIRCLE_API_KEY: 'TEST_API_KEY:x:y', CIRCLE_APP_ID: 'app-1' }

/** Start the edge as `npm run dev` does, with only `env` configured. NODE_ENV=test
 * keeps it from listening, so a start that succeeds exits 0, and one that is refused
 * exits with the reason. Nothing here reaches Circle: a refused start stops before
 * any request, and no request reaches Circle before it is needed. */
function start(env: Record<string, string>): Promise<{ code: number; stderr: string }> {
  const inherited = Object.fromEntries(
    ['PATH', 'HOME', 'TMPDIR', 'SystemRoot'].flatMap((k) =>
      process.env[k] === undefined ? [] : [[k, process.env[k] as string]],
    ),
  )
  return new Promise((resolve) => {
    execFile(
      process.execPath,
      ['--import', 'tsx', ENTRY],
      { cwd: EDGE, env: { ...inherited, NODE_ENV: 'test', ...env }, timeout: 60_000 },
      (err, _stdout, stderr) => {
        const code = err === null ? 0 : typeof err.code === 'number' ? err.code : -1
        resolve({ code, stderr })
      },
    )
  })
}

describe('starting the edge', { concurrency: true }, () => {
  it('refuses to start with nothing configured', async () => {
    const run = await start({})
    assert.notEqual(run.code, 0)
    assert.match(run.stderr, /MISTHOS_SELLER_ADDRESS/)
  })

  it('starts the gate without Circle and says the wallet routes are off', async () => {
    const run = await start({ MISTHOS_SELLER_ADDRESS: SELLER, EDGE_CORE_TOKEN: 'secret' })
    assert.equal(run.code, 0, run.stderr)
    assert.match(run.stderr, /Circle wallet routes are not mounted/)
  })

  it('starts live and names the removed switch once', async () => {
    const run = await start({
      MISTHOS_SIMULATED: 'maybe',
      MISTHOS_SELLER_ADDRESS: SELLER,
      EDGE_CORE_TOKEN: 'secret',
      ...CIRCLE,
    })
    assert.equal(run.code, 0, run.stderr)
    assert.match(run.stderr, /simulation has been removed/)
  })
})

describe('the running edge', () => {
  let facilitator: Server
  let core: Server
  let edge: Server
  let base = ''
  let coreAt = ''
  const SIGNER = '0x4444444444444444444444444444444444444444'

  before(async () => {
    // A Gateway facilitator on localhost, so nothing here reaches Circle.
    facilitator = createServer((req, res) => {
      res.setHeader('Content-Type', 'application/json')
      if (req.url === '/v1/x402/supported') {
        res.end(
          JSON.stringify({
            kinds: [
              {
                x402Version: 2,
                scheme: 'exact',
                network: 'eip155:5042002',
                extra: { verifyingContract: GATEWAY_WALLET, assets: [{ symbol: 'USDC', address: ARC_USDC }] },
              },
            ],
          }),
        )
        return
      }
      res.statusCode = 404
      res.end('{}')
    }).listen(0)
    await new Promise((resolve) => facilitator.once('listening', resolve))
    // The core's paid API, answering only the edge's token.
    core = createServer((req, res) => {
      res.setHeader('Content-Type', 'application/json')
      if (req.headers['x-misthos-core-token'] !== 'secret') {
        res.statusCode = 404
        res.end('{"detail":"Not Found"}')
        return
      }
      if (req.url === '/api/v1/internal/paid/signer') {
        res.end(JSON.stringify({ signer: SIGNER, scheme: 'eip191-personal-sign' }))
        return
      }
      res.statusCode = 404
      res.end('{}')
    }).listen(0)
    await new Promise((resolve) => core.once('listening', resolve))
    coreAt = `127.0.0.1:${(core.address() as AddressInfo).port}`
    Object.assign(process.env, {
      NODE_ENV: 'test',
      MISTHOS_SELLER_ADDRESS: SELLER,
      EDGE_CORE_TOKEN: 'secret',
      CORE_API_URL: `http://${coreAt}`,
      GATEWAY_FACILITATOR_URL: `http://127.0.0.1:${(facilitator.address() as AddressInfo).port}`,
    })
    const { default: app } = await import('./index.js')
    edge = app.listen(0)
    await new Promise((resolve) => edge.once('listening', resolve))
    base = `http://127.0.0.1:${(edge.address() as AddressInfo).port}`
  })

  after(async () => {
    for (const server of [edge, core, facilitator]) {
      server.closeAllConnections()
      await new Promise((resolve) => server.close(resolve))
    }
  })

  it('reports its health without naming the core', async () => {
    const res = await fetch(`${base}/health`)
    const text = await res.text()
    assert.equal(res.status, 200)
    assert.ok(!text.includes(coreAt), 'the core address stays inside')
    assert.doesNotMatch(text, /core/)
    assert.equal(JSON.parse(text).role, 'x402-seller')
  })

  it('publishes the catalog at /paid, with who signs its certificates', async () => {
    const res = await fetch(`${base}/paid`)
    const text = await res.text()
    const body = JSON.parse(text) as {
      protocol: string
      payTo: string
      accepts: { asset: string }[]
      certificate_signer: string
      routes: { id: string; path: string }[]
    }
    assert.equal(res.status, 200)
    assert.equal(body.protocol, 'x402')
    assert.equal(body.payTo, SELLER)
    assert.equal(body.accepts[0].asset, ARC_USDC)
    assert.equal(body.certificate_signer, SIGNER)
    assert.deepEqual(
      body.routes.map((r) => r.id),
      [
        'decisions',
        'reputation',
        'escrow-attestation',
        'price',
        'comparables',
        'review',
        'opportunities',
        'receipt',
        'screen',
      ],
    )
    assert.ok(!text.includes(coreAt))
  })

  it('asks for payment on every listed route', async () => {
    for (const [method, path] of [
      ['GET', '/paid/decisions'],
      ['GET', '/paid/reputation/CON-1'],
      ['GET', '/paid/escrow/ISS-1001/attestation'],
      ['POST', '/paid/price'],
      ['GET', '/paid/comparables'],
      ['POST', '/paid/review'],
      ['GET', '/paid/opportunities'],
      ['GET', '/paid/receipt/ISS-1004'],
    ]) {
      const res = await fetch(`${base}${path}`, {
        method,
        ...(method === 'POST' ? { headers: { 'Content-Type': 'application/json' }, body: '{}' } : {}),
      })
      assert.equal(res.status, 402, path)
      const required = JSON.parse(Buffer.from(res.headers.get('payment-required') ?? '', 'base64').toString())
      assert.equal(required.accepts[0].network, 'eip155:5042002', path)
      assert.equal(required.accepts[0].payTo, SELLER, path)
    }
  })

  it('keeps the sanctions screen off, with no price named', async () => {
    const res = await fetch(`${base}/paid/screen`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{"address":"0x0000000000000000000000000000000000000001"}',
    })
    assert.equal(res.status, 503)
    assert.equal(res.headers.get('payment-required'), null)
    const catalog = (await (await fetch(`${base}/paid`)).json()) as { routes: { id: string; available: boolean }[] }
    assert.equal(catalog.routes.find((r) => r.id === 'screen')?.available, false)
  })

  it('shows a person the seller page, from the same catalog', async () => {
    const res = await fetch(`${base}/paid`, { headers: { Accept: 'text/html' } })
    const html = await res.text()
    assert.equal(res.status, 200)
    assert.match(res.headers.get('content-type') ?? '', /text\/html/)
    assert.match(html, /Misthos paid API/)
    assert.match(html, /Arc testnet/)
    assert.match(html, new RegExp(ARC_USDC))
    assert.match(html, /<span class="api-method">POST<\/span>/)
    assert.match(html, /<code>\/paid\/review<\/code>/)
    assert.match(html, /<code>\/paid\/reputation\/\{contributor_id\}<\/code>/)
    assert.match(html, /<b>2\.00<\/b> USDC/)
    for (const group of ['Marketplace', 'Analytics', 'Trust']) assert.match(html, new RegExp(`<h2>${group}</h2>`))
    assert.match(html, new RegExp(SIGNER))
    assert.ok(!html.includes(coreAt))
  })

  it('answers a body that is not JSON, and a path that is not here, in JSON and no more', async () => {
    const bad = await fetch(`${base}/paid/price`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{not json',
    })
    const text = await bad.text()
    assert.equal(bad.status, 400)
    assert.deepEqual(JSON.parse(text), { error: 'bad_json', charged: false })
    assert.doesNotMatch(text, /at |node_modules|\/src\//)
    const missing = await fetch(`${base}/api/v1/auth/me`)
    assert.equal(missing.status, 404)
    assert.deepEqual(await missing.json(), { error: 'not_found' })
  })

  it('no longer sells the free metrics, and sells nothing it does not list', async () => {
    for (const path of ['/paid/pricing-report', '/paid/api/v1/metrics', '/paid/anything']) {
      const res = await fetch(`${base}${path}`)
      assert.equal(res.status, 404, path)
      assert.equal(res.headers.get('payment-required'), null, path)
    }
  })
})

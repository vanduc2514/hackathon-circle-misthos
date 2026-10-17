import assert from 'node:assert/strict'
import { after, before, describe, it } from 'node:test'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { Buyer } from './buyer.js'
import { STANDARD, fakeSeller, type FakeSeller } from './fake-seller.js'
import { TOOLS, createServer, readCatalog } from './tools.js'

const KEY = `0x${'59'.repeat(32)}` as const

async function connect(seller: FakeSeller, cap = '5.00', catalog: boolean = true) {
  const buyer = new Buyer({ baseUrl: seller.url, privateKey: KEY, spendCapUsdc: cap })
  const server = createServer({ buyer, catalog: catalog ? await readCatalog(seller.url) : null, pollMs: 5 })
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
  const client = new Client({ name: 'test', version: '0.0.0' })
  await Promise.all([server.connect(serverSide), client.connect(clientSide)])
  return { client, buyer, close: () => client.close() }
}

const parse = (res: unknown) => JSON.parse((res as { content: { text: string }[] }).content[0].text)

describe('the MCP server', () => {
  let seller: FakeSeller
  before(async () => {
    seller = await fakeSeller(STANDARD)
  })
  after(() => seller.close())

  it('offers a tool for every route the seller lists, with its price, and none it does not', async () => {
    const { client, close } = await connect(seller)
    const { tools } = await client.listTools()
    await close()
    const names = tools.map((t) => t.name).sort()
    assert.deepEqual(names, [
      'misthos_contributor_reputation',
      'misthos_escrow_attestation',
      'misthos_price_issue',
      'misthos_review_diff',
      'misthos_review_result',
      'misthos_spending',
    ])
    const reputation = tools.find((t) => t.name === 'misthos_contributor_reputation')
    assert.match(reputation?.description ?? '', /Costs 0\.05 USDC a call on Arc testnet/)
    assert.match(reputation?.description ?? '', /Example: \{"contributor_id":"CON-1"\}/)
    assert.deepEqual(reputation?.inputSchema.required, ['contributor_id'])
    assert.equal(reputation?.annotations?.readOnlyHint, true)
  })

  it('maps every tool to a paid route the seller sells', () => {
    const routes = new Set(['decisions', 'reputation', 'escrow-attestation', 'price', 'comparables', 'review', 'opportunities', 'receipt'])
    for (const tool of TOOLS) assert.ok(routes.has(tool.route), tool.name)
    assert.ok(!TOOLS.some((t) => t.route === 'screen'), 'the screen is off, so it is no tool')
  })

  it('pays for a call and shows the settlement', async () => {
    const { client, close } = await connect(seller)
    const res = await client.callTool({ name: 'misthos_contributor_reputation', arguments: { contributor_id: 'CON 7' } })
    const spending = parse(await client.callTool({ name: 'misthos_spending', arguments: {} }))
    await close()
    const body = parse(res)
    assert.equal(res.isError, undefined)
    assert.deepEqual(body.result, { contributor_id: 'CON 7', score: 42 })
    assert.equal(body.paid.usdc, '0.05')
    assert.match(body.paid.transaction, /^batch-/)
    assert.equal(spending.spent, '0.05')
    assert.equal(spending.network, 'eip155:5042002')
  })

  it('waits for a review and returns the verdict, paid once', async () => {
    const { client, buyer, close } = await connect(seller)
    const res = await client.callTool({
      name: 'misthos_review_diff',
      arguments: { criteria: ['Covered by a test'], diff: 'diff --git a/x b/x', wait_seconds: 5 },
    })
    await close()
    const body = parse(res)
    assert.equal(body.result.verdict, 'accept')
    assert.equal(body.paid.usdc, '2.00')
    assert.equal(buyer.spend.summary().spent, '2.00')
    assert.equal(buyer.spend.summary().held, '0.00')
  })

  it('returns a review still running as a job to collect later', async () => {
    const { client, close } = await connect(seller)
    const started = parse(
      await client.callTool({
        name: 'misthos_review_diff',
        arguments: { criteria: ['Covered by a test'], diff: 'diff --git a/x b/x', wait_seconds: 0 },
      }),
    )
    assert.equal(started.status, 'running')
    assert.ok(started.job_id && started.receipt)
    const first = parse(await client.callTool({ name: 'misthos_review_result', arguments: { job_id: started.job_id, receipt: started.receipt } }))
    const second = parse(await client.callTool({ name: 'misthos_review_result', arguments: { job_id: started.job_id, receipt: started.receipt } }))
    await close()
    assert.equal(first.result.status, 'running')
    assert.equal(second.result.verdict, 'accept')
    assert.equal(second.paid.usdc, '2.00')
  })

  it('answers a refusal as a machine-readable error with nothing internal in it', async () => {
    const tight = await connect(seller, '0.01')
    const capped = await tight.client.callTool({ name: 'misthos_price_issue', arguments: { title: 't', repo: 'a/b' } })
    await tight.close()
    assert.equal(capped.isError, true)
    assert.equal(parse(capped).error, 'spend_cap_exceeded')
    assert.equal(parse(capped).charged, false)

    const { client, close } = await connect(seller)
    const failed = await client.callTool({ name: 'misthos_escrow_attestation', arguments: { issue_id: 'ISS-1' } })
    await close()
    const text = JSON.stringify(parse(failed))
    assert.equal(failed.isError, true)
    assert.equal(parse(failed).error, 'chain_unavailable')
    assert.equal(parse(failed).charged, false)
    assert.doesNotMatch(text, /10\.1\.2\.3|127\.0\.0\.1|http/)
  })

  it('still offers its tools when the catalog cannot be read, priced at call time', async () => {
    const { client, close } = await connect(seller, '5.00', false)
    const { tools } = await client.listTools()
    await close()
    assert.equal(tools.length, TOOLS.length + 2)
    assert.match(tools[0].description ?? '', /Priced by the seller at call time/)
  })
})

import { describe, expect, it } from 'vitest'
import {
  exampleCall,
  freeEndpoints,
  grouped,
  paidEndpoints,
  returnsOf,
  shown,
  type PaidCatalog,
} from './public-api'

const schema = {
  info: { version: '0.1.0' },
  paths: {
    '/api/v1/health': {
      get: {
        'x-misthos-public': { group: 'Core', name: 'Health', summary: 'Network and money.' },
        responses: { '200': { content: { 'application/json': { schema: { $ref: '#/components/schemas/HealthOut' } } } } },
      },
    },
    '/api/v1/issues/{issue_id}/timeline': {
      get: {
        'x-misthos-public': { group: 'Marketplace', name: 'Issue timeline', summary: 'The audit trail.' },
        responses: {
          '200': {
            content: {
              'application/json': { schema: { type: 'array', items: { $ref: '#/components/schemas/TimelineEntry' } } },
            },
          },
        },
      },
    },
    '/api/v1/auth/me': { get: { responses: {} } },
    '/api/v1/publishers/{publisher_id}/audit': {
      get: { 'x-misthos-public': { group: 'Nowhere' }, responses: {} },
    },
  },
}

const catalog: PaidCatalog = {
  available: true,
  base_url: 'https://paid.example.com',
  networks: ['eip155:5042002'],
  routes: [
    {
      id: 'reputation',
      method: 'GET',
      path: '/paid/reputation/:contributor_id',
      price: { usdc: '0.05', base_units: '50000' },
      name: 'Contributor standing',
      summary: 'Score and signals.',
      group: 'Trust',
      returns: 'Reputation',
      free_tier: true,
      async: false,
      capped: false,
    },
    {
      id: 'review',
      method: 'POST',
      path: '/paid/review',
      price: { usdc: '2.00', base_units: '2000000' },
      name: 'Pre-flight review',
      summary: 'A verdict on a diff.',
      group: 'Analytics',
      free_tier: false,
      async: true,
      capped: true,
      note: 'the cap is the price',
    },
  ],
}

describe('the public API catalogue', () => {
  it('lists only the routes the schema marks public, with what they return', () => {
    const free = freeEndpoints(schema)
    expect(free.map((e) => e.path)).toEqual(['/api/v1/health', '/api/v1/issues/{issue_id}/timeline'])
    expect(free[0]).toMatchObject({ method: 'GET', name: 'Health', group: 'Core', returns: 'HealthOut', price: null })
    expect(free[1].returns).toBe('TimelineEntry[]')
  })

  it('lists the paid routes at the edge price, written the way a reader writes a path', () => {
    const paid = paidEndpoints(catalog)
    expect(paid.map((e) => [e.path, e.price, e.group])).toEqual([
      ['/paid/reputation/{contributor_id}', '0.05', 'Trust'],
      ['/paid/review', '2.00', 'Analytics'],
    ])
    expect(paid[1]).toMatchObject({ capped: true, async: true, note: 'the cap is the price' })
  })

  it('shows no paid route when the edge could not be read', () => {
    expect(paidEndpoints({ ...catalog, available: false })).toEqual([])
    expect(paidEndpoints(null)).toEqual([])
  })

  it('filters by price and groups in page order', () => {
    const all = [...freeEndpoints(schema), ...paidEndpoints(catalog)]
    expect(shown(all, 'all')).toHaveLength(4)
    expect(shown(all, 'free').every((e) => !e.price)).toBe(true)
    expect(shown(all, 'x402').map((e) => e.name)).toEqual(['Contributor standing', 'Pre-flight review'])
    expect(grouped(all).map((g) => [g.group, g.rows.length])).toEqual([
      ['Core', 1],
      ['Marketplace', 1],
      ['Analytics', 1],
      ['Trust', 1],
    ])
  })

  it('writes a call against the host that serves it', () => {
    const [health] = freeEndpoints(schema)
    const [reputation, review] = paidEndpoints(catalog)
    expect(exampleCall(health, 'https://misthos.example', null)).toBe("curl -i 'https://misthos.example/api/v1/health'")
    expect(exampleCall(reputation, 'https://misthos.example', 'https://paid.example.com/')).toBe(
      "curl -i 'https://paid.example.com/paid/reputation/{contributor_id}'",
    )
    expect(exampleCall(review, 'x', null)).toMatch(/^curl -i -X POST '\$MISTHOS_PAID_URL\/paid\/review'/)
  })

  it('reads a return type from a reference or an array of one', () => {
    expect(returnsOf({ $ref: '#/components/schemas/IssueOut' })).toBe('IssueOut')
    expect(returnsOf({ type: 'array', items: { $ref: '#/a/Decision' } })).toBe('Decision[]')
    expect(returnsOf({ type: 'object' })).toBeNull()
    expect(returnsOf(undefined)).toBeNull()
  })
})

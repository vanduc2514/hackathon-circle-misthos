/**
 * The public API page's catalogue (#208), from the two places that know it.
 *
 * The free routes come from the API's own schema: an operation is public when it
 * carries `x-misthos-public`, which the API puts only on routes a stranger may call.
 * The x402 routes come from the edge's catalog, read through the API, so a price on the
 * page is the price the edge charges. Nothing here is written down a second time.
 */

import type { components } from './api-schema'

export type PaidCatalog = components['schemas']['PaidCatalogOut']
export type Group = 'Core' | 'Marketplace' | 'Analytics' | 'Trust'
export const GROUPS: Group[] = ['Core', 'Marketplace', 'Analytics', 'Trust']
export type Tab = 'all' | 'free' | 'x402'

export interface Endpoint {
  key: string
  method: string
  /** As a reader writes it: `{issue_id}`, never `:issue_id`. */
  path: string
  name: string
  summary: string
  group: Group
  returns: string | null
  /** USDC, when the route is sold over x402. */
  price: string | null
  capped: boolean
  async: boolean
  freeTier: boolean
  note: string | null
}

interface Operation {
  'x-misthos-public'?: { group?: string; name?: string; summary?: string }
  responses?: Record<string, { content?: Record<string, { schema?: unknown }> }>
}

export interface ApiSchema {
  openapi?: string
  info?: { version?: string }
  paths?: Record<string, Record<string, Operation>>
}

const isGroup = (value: unknown): value is Group => GROUPS.includes(value as Group)

/** `IssueOut`, `TimelineEntry[]`: what a route answers, from its 200 schema. */
export function returnsOf(schema: unknown): string | null {
  if (!schema || typeof schema !== 'object') return null
  const s = schema as { $ref?: string; type?: string; items?: unknown }
  if (s.$ref) return s.$ref.split('/').pop() ?? null
  if (s.type === 'array') {
    const inner = returnsOf(s.items)
    return inner ? `${inner}[]` : null
  }
  return null
}

/** Every route the API marks public, in the order its schema lists them. */
export function freeEndpoints(schema: ApiSchema | null | undefined): Endpoint[] {
  const out: Endpoint[] = []
  for (const [path, ops] of Object.entries(schema?.paths ?? {})) {
    for (const [method, op] of Object.entries(ops ?? {})) {
      const marker = op?.['x-misthos-public']
      if (!marker || !isGroup(marker.group)) continue
      const ok = op.responses?.['200']?.content?.['application/json']?.schema
      out.push({
        key: `${method.toUpperCase()} ${path}`,
        method: method.toUpperCase(),
        path,
        name: marker.name ?? path,
        summary: marker.summary ?? '',
        group: marker.group,
        returns: returnsOf(ok),
        price: null,
        capped: false,
        async: false,
        freeTier: false,
        note: null,
      })
    }
  }
  return out
}

/** Every route the edge sells and has on, at the price it charges. */
export function paidEndpoints(catalog: PaidCatalog | null | undefined): Endpoint[] {
  if (!catalog?.available) return []
  return (catalog.routes ?? []).map((r) => ({
    key: `${r.method} ${r.path}`,
    method: r.method,
    path: r.path.replace(/:(\w+)/g, '{$1}'),
    name: r.name,
    summary: r.summary,
    group: isGroup(r.group) ? r.group : 'Analytics',
    returns: r.returns ?? null,
    price: r.price.usdc ?? null,
    capped: r.capped,
    async: r.async,
    freeTier: r.free_tier,
    note: r.note ?? null,
  }))
}

export function shown(endpoints: Endpoint[], tab: Tab): Endpoint[] {
  if (tab === 'free') return endpoints.filter((e) => !e.price)
  if (tab === 'x402') return endpoints.filter((e) => e.price)
  return endpoints
}

/** The groups in page order, each with its rows; a group with none is left out. */
export function grouped(endpoints: Endpoint[]): { group: Group; rows: Endpoint[] }[] {
  return GROUPS.map((group) => ({ group, rows: endpoints.filter((e) => e.group === group) })).filter(
    (g) => g.rows.length > 0,
  )
}

/** A call a reader can paste, against the right host. */
export function exampleCall(endpoint: Endpoint, apiOrigin: string, paidBase: string | null): string {
  const base = endpoint.price ? (paidBase ?? '$MISTHOS_PAID_URL') : apiOrigin
  const url = `${base.replace(/\/+$/, '')}${endpoint.path}`
  if (endpoint.method === 'GET') return `curl -i '${url}'`
  return `curl -i -X POST '${url}' \\\n  -H 'Content-Type: application/json' \\\n  -d '{…}'`
}

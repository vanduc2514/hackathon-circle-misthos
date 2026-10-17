/**
 * The paid API as MCP tools (#205). One tool per paid route, each with a typed input,
 * a description that names its price, and the route it calls. The prices come from
 * the seller's own catalog (`GET /paid`) when the server starts, so a tool never
 * claims a price the seller does not charge, and a route the seller does not list,
 * or lists as off, is not offered.
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { z } from 'zod'
import type { Buyer, Outcome } from './buyer.js'

const SIGNALS = [
  'code_surface',
  'requirement_clarity',
  'test_coverage',
  'dependency_depth',
  'prior_attempts',
  'blast_radius',
] as const

const signal = z.number().min(1).max(5)
const signals = z.object(Object.fromEntries(SIGNALS.map((s) => [s, signal.optional()]))).strict()

export interface CatalogRoute {
  id: string
  method: string
  path: string
  price: { usdc?: string; base_units: string }
  available?: boolean
  async?: boolean
}

export interface Catalog {
  networks?: string[]
  accepts?: { network: string; asset: string }[]
  routes: CatalogRoute[]
}

type Args = Record<string, unknown>

interface Tool {
  name: string
  route: string
  title: string
  description: string
  input: z.ZodRawShape
  example: Args
  /** The path and query, or body, this tool's arguments make. */
  request: (args: Args) => { path: string; body?: unknown }
}

function query(args: Args, keys: readonly string[]): string {
  const params = new URLSearchParams()
  for (const key of keys) {
    const value = args[key]
    if (value === undefined || value === null || value === '') continue
    params.set(key, Array.isArray(value) ? value.join(',') : String(value))
  }
  const text = params.toString()
  return text ? `?${text}` : ''
}

const seg = (value: unknown) => encodeURIComponent(String(value))

export const TOOLS: Tool[] = [
  {
    name: 'misthos_price_issue',
    route: 'price',
    title: 'Price a GitHub issue',
    description:
      "Ask the Misthos pricing engine what an issue is worth: a recommended USDC price, a range, the confidence and why, the complexity signals it read, the settled comparables that moved it, and the justification. Advice only; it changes nothing.",
    input: {
      title: z.string().min(1).max(300),
      body: z.string().max(20_000).optional(),
      repo: z.string().describe('owner/name or a github.com link'),
      labels: z.array(z.string().max(50)).max(20).optional(),
      signals: signals.optional().describe('Any of the six signals, 1 to 5, to set over what is read'),
      compliance_driven: z.boolean().optional(),
      prior_attempts: z.number().int().min(0).max(100).optional(),
    },
    example: { title: 'Retry webhook deliveries that time out', repo: 'acme/hooks', labels: ['bug'] },
    request: (args) => ({ path: '/paid/price', body: args }),
  },
  {
    name: 'misthos_find_comparables',
    route: 'comparables',
    title: 'Find settled comparables',
    description:
      'Settled Misthos issues with their price, settled date, repo, and, given all six signals, the distance and the price each implies for work of that shape.',
    input: {
      repo: z.string().optional(),
      labels: z.array(z.string()).optional(),
      signals: z
        .object(Object.fromEntries(SIGNALS.map((s) => [s, signal])))
        .strict()
        .optional()
        .describe('All six signals, 1 to 5, or leave it out'),
      limit: z.number().int().min(1).max(100).optional(),
      cursor: z.string().optional(),
    },
    example: { repo: 'acme/hooks', labels: ['bug'], limit: 20 },
    request: (args) => {
      const flat: Args = { ...args, ...((args.signals as Args | undefined) ?? {}) }
      return { path: `/paid/comparables${query(flat, ['repo', 'labels', 'limit', 'cursor', ...SIGNALS])}` }
    },
  },
  {
    name: 'misthos_review_diff',
    route: 'review',
    title: 'Pre-flight review of a diff',
    description:
      'Have the Misthos review agent hold a unified diff, or a pull request, to acceptance criteria before you submit it: verdict (accept, rework, reject), findings, and per criterion whether it is met with the evidence. Runs as a job; this tool waits up to wait_seconds, then returns the job to collect with misthos_review_result. Charged only when the finished review is delivered.',
    input: {
      criteria: z.array(z.string().min(1).max(500)).min(1).max(20),
      diff: z.string().max(200_000).optional(),
      pull_request: z.object({ repo: z.string(), number: z.number().int().positive() }).optional(),
      title: z.string().max(300).optional(),
      repo: z.string().optional(),
      checks_passed: z.boolean().optional(),
      wait_seconds: z.number().int().min(0).max(300).optional(),
    },
    example: { criteria: ['Covered by a test'], diff: 'diff --git a/tests/test_x.py b/tests/test_x.py\n…' },
    request: (args) => {
      const { wait_seconds: _wait, ...body } = args
      return { path: '/paid/review', body }
    },
  },
  {
    name: 'misthos_contributor_reputation',
    route: 'reputation',
    title: "A contributor's reputation",
    description:
      "A Misthos contributor's standing from the money ledger: points, a 0 to 100 score, percentile, verdicts, rework and disputes, and the repos and labels of their settled work. Aggregates only.",
    input: { contributor_id: z.string().min(1).max(32) },
    example: { contributor_id: 'CON-1' },
    request: (args) => ({ path: `/paid/reputation/${seg(args.contributor_id)}` }),
  },
  {
    name: 'misthos_list_opportunities',
    route: 'opportunities',
    title: 'Funded work open now',
    description:
      'Funded Misthos issues still open: price, what a contributor receives, deadline, claim state, repo, labels and acceptance criteria.',
    input: {
      repo: z.string().optional(),
      labels: z.array(z.string()).optional(),
      min_price: z.string().optional().describe('USDC'),
      max_price: z.string().optional().describe('USDC'),
      claimable: z.boolean().optional(),
      sort: z.enum(['newest', 'price', 'deadline']).optional(),
      limit: z.number().int().min(1).max(100).optional(),
      cursor: z.string().optional(),
    },
    example: { claimable: true, sort: 'price' },
    request: (args) => ({
      path: `/paid/opportunities${query(args, ['repo', 'labels', 'min_price', 'max_price', 'claimable', 'sort', 'limit', 'cursor'])}`,
    }),
  },
  {
    name: 'misthos_escrow_attestation',
    route: 'escrow-attestation',
    title: 'Signed escrow attestation',
    description:
      'A signed certificate of what MisthosEscrow on Arc holds for an issue: amount, status, deadline, publisher, contract and commitment transaction, read from the chain.',
    input: { issue_id: z.string().min(1).max(32) },
    example: { issue_id: 'ISS-1001' },
    request: (args) => ({ path: `/paid/escrow/${seg(args.issue_id)}/attestation` }),
  },
  {
    name: 'misthos_settlement_receipt',
    route: 'receipt',
    title: 'Signed settlement receipt',
    description:
      "A signed receipt of an issue's settlement: released or refunded, gross, the platform fee, net, and when, checked against the money ledger and the escrow. The settlement id is the issue id.",
    input: { settlement_id: z.string().min(1).max(32) },
    example: { settlement_id: 'ISS-1004' },
    request: (args) => ({ path: `/paid/receipt/${seg(args.settlement_id)}` }),
  },
  {
    name: 'misthos_decision_log',
    route: 'decisions',
    title: 'The decision log',
    description:
      "Misthos's append-only decision log, oldest first, a page at a time: who decided what on which issue, by which rule.",
    input: {
      issue_id: z.string().optional(),
      actor: z.enum(['agent', 'publisher', 'contributor', 'system']).optional(),
      action: z.string().optional(),
      since: z.string().optional().describe('ISO 8601'),
      until: z.string().optional().describe('ISO 8601'),
      limit: z.number().int().min(1).max(200).optional(),
      cursor: z.string().optional(),
    },
    example: { actor: 'agent', limit: 50 },
    request: (args) => ({
      path: `/paid/decisions${query(args, ['issue_id', 'actor', 'action', 'since', 'until', 'limit', 'cursor'])}`,
    }),
  },
]

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

function result(outcome: Outcome, extra: Args = {}) {
  const payload = outcome.ok
    ? { ...extra, ...(outcome.paid ? { paid: outcome.paid } : {}), ...(outcome.held ? { held: outcome.held } : {}), result: outcome.data }
    : { ...extra, error: outcome.error, ...(outcome.detail ? { detail: outcome.detail } : {}), charged: outcome.charged }
  return {
    content: [{ type: 'text' as const, text: JSON.stringify(payload, null, 2) }],
    structuredContent: payload as Record<string, unknown>,
    ...(outcome.ok ? {} : { isError: true }),
  }
}

/** Fetch the seller's catalog. Null when it cannot be read. */
export async function readCatalog(baseUrl: string, http: typeof fetch = fetch): Promise<Catalog | null> {
  try {
    const res = await http(`${baseUrl.replace(/\/+$/, '')}/paid`, {
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(10_000),
    })
    return res.ok ? ((await res.json()) as Catalog) : null
  } catch {
    return null
  }
}

export interface ServerOptions {
  buyer: Buyer
  catalog: Catalog | null
  /** How often a waiting review is polled. */
  pollMs?: number
}

export function createServer({ buyer, catalog, pollMs = 3000 }: ServerOptions): McpServer {
  const server = new McpServer({ name: 'misthos', version: '0.1.0' })
  const listed = new Map((catalog?.routes ?? []).map((r) => [r.id, r]))

  for (const tool of TOOLS) {
    const route = listed.get(tool.route)
    if (catalog && (!route || route.available === false)) continue
    const price = route?.price.usdc
    const priced = price
      ? ` Costs ${price} USDC a call on Arc testnet, paid from your Gateway balance; nothing if the call fails.`
      : ' Priced by the seller at call time.'
    server.registerTool(
      tool.name,
      {
        title: tool.title,
        description: `${tool.description}${priced} Example: ${JSON.stringify(tool.example)}`,
        inputSchema: tool.input,
        annotations: { readOnlyHint: true, openWorldHint: true },
      },
      async (args: Args) => {
        const { path, body } = tool.request(args)
        const method = body === undefined ? 'GET' : 'POST'
        const outcome = await buyer.call(method, path, body)
        if (tool.route !== 'review' || !outcome.ok || outcome.status !== 202) return result(outcome)

        const job = outcome.data as { job_id: string; receipt: string; poll: string }
        const deadline = Date.now() + Number(args.wait_seconds ?? 60) * 1000
        while (Date.now() < deadline) {
          await sleep(pollMs)
          const polled = await buyer.collect(job.poll, job.job_id, job.receipt)
          if (!polled.ok || polled.status !== 202) return result(polled, { job_id: job.job_id })
        }
        return result(outcome, {
          job_id: job.job_id,
          receipt: job.receipt,
          status: 'running',
          next: 'call misthos_review_result with job_id and receipt',
        })
      },
    )
  }

  if (!catalog || listed.has('review')) {
    server.registerTool(
      'misthos_review_result',
      {
        title: 'Collect a review',
        description:
          'Collect a pre-flight review started by misthos_review_diff. Free to poll; the review is paid for once, when it is delivered.',
        inputSchema: { job_id: z.string().min(1), receipt: z.string().min(1) },
        annotations: { readOnlyHint: true, openWorldHint: true },
      },
      async (args: Args) =>
        result(await buyer.collect(`/paid/review/jobs/${seg(args.job_id)}`, String(args.job_id), String(args.receipt)), {
          job_id: args.job_id,
        }),
    )
  }

  server.registerTool(
    'misthos_spending',
    {
      title: 'What this session has spent',
      description: 'USDC spent, held for reviews not yet collected, the spend cap and what is left. Free.',
      inputSchema: {},
      annotations: { readOnlyHint: true },
    },
    async () => {
      const payload = {
        wallet: buyer.address,
        network: buyer.network,
        ...buyer.spend.summary(),
      }
      return {
        content: [{ type: 'text' as const, text: JSON.stringify(payload, null, 2) }],
        structuredContent: payload,
      }
    },
  )
  return server
}

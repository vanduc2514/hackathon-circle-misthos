/**
 * Misthos edge service.
 *
 * This process exists for two reasons, both of which are TypeScript-only in
 * Circle's stack:
 *
 *   1. The x402 seller rail. Circle's Gateway Nanopayments client ships as
 *      `@circle-fin/x402-batching` for Node. Their own guidance for FastAPI and
 *      other non-Node APIs is a thin proxy in front, which is this file: the paid
 *      routes in `catalog.ts`, each forwarded to the core's internal paid API.
 *   2. Agent wallet operations, which Circle documents around the Circle CLI.
 *
 * Keep it small. Anything that is not one of those two jobs belongs in the
 * Python core.
 */

import express, { type NextFunction, type Request, type Response } from 'express'
import { CATALOG } from './catalog.js'
import { CORE_PAID_PREFIX, catalogOf, coreClient, freeApiKeys, paidRouter, prices } from './paid.js'
import { sellerPage } from './seller-page.js'
import { createRail } from './x402-gate.js'
import { walletStatus, type WalletOp } from './circle-cli.js'
import { createWalletService } from './circle-wallets.js'
import { walletRouter } from './wallet-routes.js'

let warnedLegacySwitch = false

/** Name the removed switch once; a deployment that still sets it runs live anyway. */
function warnLegacySwitch(env: NodeJS.ProcessEnv = process.env): void {
  if (env.MISTHOS_SIMULATED === undefined || warnedLegacySwitch) return
  warnedLegacySwitch = true
  console.warn(
    'MISTHOS_SIMULATED is set but simulation has been removed; the edge runs live and ignores it',
  )
}

const app = express()
app.use(express.json({ limit: '512kb' }))

const PORT = Number(process.env.EDGE_PORT ?? 8080)
const CORE_URL = process.env.CORE_API_URL ?? 'http://127.0.0.1:8000'
const CORE_TOKEN = process.env.EDGE_CORE_TOKEN ?? ''
warnLegacySwitch()
const rail = createRail()
const priced = prices(CATALOG)
if (CATALOG.length > 0 && !CORE_TOKEN) {
  throw new Error('EDGE_CORE_TOKEN is required: the paid routes run in the core, which only answers the edge')
}

/** Health, used by the compose file and by `mise run dev` sanity checks. It names no
 * internal address: the edge is public. */
app.get('/health', (_req: Request, res: Response) => {
  res.json({
    status: 'ok',
    service: 'misthos-edge',
    role: 'x402-seller',
    networks: rail.config.networks,
    facilitator: rail.config.facilitatorUrl,
    paid_routes: CATALOG.length,
  })
})

const core = coreClient(CORE_URL, CORE_TOKEN)

/** The address certificates are signed by, asked of the core and kept a while. */
let signer: { value: string | null; at: number } | null = null
async function certificateSigner(): Promise<string | null> {
  const fresh = signer && Date.now() - signer.at < (signer.value ? 10 * 60_000 : 60_000)
  if (fresh && signer) return signer.value
  const out = await core.call({ method: 'GET', path: `${CORE_PAID_PREFIX}signer` })
  const found = (out.body as { signer?: unknown }).signer
  signer = { value: out.status === 200 && typeof found === 'string' ? found : null, at: Date.now() }
  return signer.value
}

/** The seller page: every paid route, its price, how to pay, and who signs the
 * certificates it sells. Free. JSON for an agent, HTML for a person. */
app.get('/paid', async (req: Request, res: Response) => {
  const catalog = await catalogOf(rail, CATALOG, priced, {
    certificate_signer: await certificateSigner(),
  })
  if (req.accepts(['application/json', 'text/html']) === 'text/html') {
    res.type('html').send(sellerPage(catalog as unknown as Parameters<typeof sellerPage>[0]))
    return
  }
  res.json(catalog)
})

/**
 * The paid routes. Each answers 402 with the x402 price list until the request carries
 * a payment, verifies it, runs the work in the core, and settles only if the work
 * succeeded. Only what `catalog.ts` lists is served; nothing wraps `/api/v1/*`.
 */
app.use(
  paidRouter({
    rail,
    routes: CATALOG,
    core,
    prices: priced,
    apiKeys: freeApiKeys(),
  }),
)

/** Anything else under /paid is not for sale. */
app.use('/paid', (_req: Request, res: Response) => {
  res.status(404).json({ error: 'not_a_paid_route', catalog: '/paid' })
})

/**
 * Circle user-controlled wallets for publishers and contributors. Core only.
 *
 * The x402 gate does not need Circle, and the wallet routes are a separate concern on
 * the same service, so they are mounted only when Circle is configured. Without the
 * credentials the gate still runs and the routes are absent rather than half-working.
 */
const circleConfigured = Boolean(process.env.CIRCLE_API_KEY && process.env.CIRCLE_APP_ID)
if (circleConfigured) {
  app.use('/wallets', walletRouter(createWalletService()))
} else {
  console.warn('CIRCLE_API_KEY and CIRCLE_APP_ID are unset; Circle wallet routes are not mounted')
}

/** Wallet operations the Python core delegates here because the CLI is Node. */
app.get('/wallet/status', async (_req: Request, res: Response) => {
  res.json(await walletStatus())
})

app.post('/wallet/:op', async (req: Request, res: Response) => {
  const allowed: WalletOp[] = ['transfer', 'bridge', 'swap', 'limit']
  const op = req.params.op as WalletOp
  if (!allowed.includes(op)) {
    res.status(400).json({ error: `unsupported operation ${op}`, allowed })
    return
  }
  res.json({ accepted: true, op, note: 'Circle CLI call would run here.' })
})

/** Anything else is not here, in JSON like every other answer. */
app.use((_req: Request, res: Response) => {
  res.status(404).json({ error: 'not_found' })
})

/**
 * What a handler did not catch: a body that is not JSON, or a bug. The answer says
 * which in a word and nothing more: no stack, no path, no address of anything behind
 * the edge.
 */
app.use((err: unknown, _req: Request, res: Response, _next: NextFunction) => {
  const parse = (err as { type?: string }).type === 'entity.parse.failed'
  const tooLarge = (err as { type?: string }).type === 'entity.too.large'
  if (!parse && !tooLarge) console.error(JSON.stringify({ event: 'edge.error', error: String(err) }))
  if (res.headersSent) return
  if (parse) res.status(400).json({ error: 'bad_json', charged: false })
  else if (tooLarge) res.status(413).json({ error: 'too_large', charged: false })
  else res.status(500).json({ error: 'internal_error', charged: false })
})

if (process.env.NODE_ENV !== 'test') {
  app.listen(PORT, () => {
    console.log(`misthos-edge listening on http://localhost:${PORT}`)
    console.log(
      `  x402 settles through ${rail.config.facilitatorUrl} on ${rail.config.networks.join(', ')}`,
    )
    console.log(`  ${CATALOG.length} paid routes; the catalog is at /paid`)
  })
}

export default app

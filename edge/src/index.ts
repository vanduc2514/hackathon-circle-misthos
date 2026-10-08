/**
 * Misthos edge service.
 *
 * This process exists for two reasons, both of which are TypeScript-only in
 * Circle's stack:
 *
 *   1. The x402 seller gate. Circle's Gateway Nanopayments middleware ships as
 *      `@circle-fin/x402-batching` for Node. Their own guidance for FastAPI and
 *      other non-Node APIs is a thin proxy in front, which is this file.
 *   2. Agent wallet operations, which Circle documents around the Circle CLI.
 *
 * Keep it small. Anything that is not one of those two jobs belongs in the
 * Python core.
 */

import express, { type Request, type Response } from 'express'
import { createGate, settlementOf } from './x402-gate.js'
import { walletStatus, type WalletOp } from './circle-cli.js'
import { createWalletService } from './circle-wallets.js'
import { walletRouter } from './wallet-routes.js'

const app = express()
app.use(express.json({ limit: '512kb' }))

const PORT = Number(process.env.EDGE_PORT ?? 8080)
const CORE_URL = process.env.CORE_API_URL ?? 'http://127.0.0.1:8000'
const gate = createGate()

/** Health, used by the compose file and by `mise run dev` sanity checks. */
app.get('/health', (_req: Request, res: Response) => {
  res.json({
    status: 'ok',
    service: 'misthos-edge',
    role: 'x402-gate',
    core: CORE_URL,
    simulated: gate.config.simulated,
    networks: gate.config.networks,
    facilitator: gate.config.simulated ? null : gate.config.facilitatorUrl,
  })
})

/**
 * A metered endpoint. Unpaid, it answers 402 with the machine-readable price
 * list x402 specifies, so an agent can pick a rail, sign and retry. Paid, the
 * gate has already settled before this handler runs.
 */
app.get('/paid/pricing-report', gate.require('0.25'), (_req: Request, res: Response) => {
  const settlement = settlementOf(res)
  // Forward the paid request to the Python core, which is unchanged by any of
  // this. The gate is the only thing the edge service adds.
  void fetch(`${CORE_URL}/api/v1/metrics`)
    .then((r) => r.json())
    .then((metrics) => res.json({ settled: true, ...settlement, metrics }))
    .catch((err: unknown) =>
      res.status(502).json({ error: 'core unavailable', detail: String(err) }),
    )
})

/** Circle user-controlled wallets for publishers and contributors. Core only. */
app.use('/wallets', walletRouter(createWalletService()))

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
  res.json({ accepted: true, op, simulated: true, note: 'Circle CLI call would run here.' })
})

if (process.env.NODE_ENV !== 'test') {
  app.listen(PORT, () => {
    console.log(`misthos-edge listening on http://localhost:${PORT}`)
    console.log(`  core: ${CORE_URL}`)
    console.log(
      gate.config.simulated
        ? '  simulated: no chain is contacted'
        : `  x402 settles through ${gate.config.facilitatorUrl} on ${gate.config.networks.join(', ')}`,
    )
  })
}

export default app

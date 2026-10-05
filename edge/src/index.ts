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
import { paymentRequirements, verifyAndSettle, type PaymentProof } from './x402-gate.js'
import { walletStatus, type WalletOp } from './circle-cli.js'

const app = express()
app.use(express.json({ limit: '512kb' }))

const PORT = Number(process.env.EDGE_PORT ?? 8080)
const CORE_URL = process.env.CORE_API_URL ?? 'http://127.0.0.1:8000'

/** Health, used by the compose file and by `mise run dev` sanity checks. */
app.get('/health', (_req: Request, res: Response) => {
  res.json({
    status: 'ok',
    service: 'misthos-edge',
    role: 'x402-gate',
    core: CORE_URL,
    simulated: true,
  })
})

/**
 * The unpaid probe. An agent that calls this without payment gets the machine
 * readable price list x402 specifies, so it can pick a rail and retry.
 */
app.get('/paid/pricing-report', (req: Request, res: Response) => {
  const paid = Boolean(req.header('X-PAYMENT'))
  if (!paid) {
    res.status(402).json({
      error: 'payment required',
      accepts: paymentRequirements('/paid/pricing-report', '0.25'),
    })
    return
  }

  const proof: PaymentProof = {
    raw: req.header('X-PAYMENT') ?? '',
    resource: '/paid/pricing-report',
  }

  void verifyAndSettle(proof).then((result) => {
    if (!result.settled) {
      res.status(402).json({ error: result.reason, accepts: paymentRequirements('/paid/pricing-report', '0.25') })
      return
    }
    // Forward the paid request to the Python core, which is unchanged by any of
    // this. The gate is the only thing the edge service adds.
    void fetch(`${CORE_URL}/api/v1/metrics`)
      .then((r) => r.json())
      .then((metrics) =>
        res.json({
          settled: true,
          payer: result.payer,
          rail: result.rail,
          metrics,
        }),
      )
      .catch((err: unknown) =>
        res.status(502).json({ error: 'core unavailable', detail: String(err) }),
      )
  })
})

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
    console.log('  simulated: no chain is contacted')
  })
}

export default app

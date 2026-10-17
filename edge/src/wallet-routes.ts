/**
 * Wallet routes the Python core calls.
 *
 * The edge is public because x402 buyers reach it, but these routes hand out
 * Circle user tokens, so only the core may call them: every request must carry the
 * shared EDGE_CORE_TOKEN.
 */

import { Router, type NextFunction, type Request, type Response } from 'express'
import { timingSafeEqual } from 'node:crypto'
import type { Party, WalletService } from './circle-wallets.js'

const PARTIES: Party[] = ['publisher', 'contributor']

function sameSecret(a: string, b: string): boolean {
  const left = Buffer.from(a)
  const right = Buffer.from(b)
  return left.length === right.length && timingSafeEqual(left, right)
}

export function walletRouter(service: WalletService, coreToken = process.env.EDGE_CORE_TOKEN ?? '') {
  if (!coreToken) {
    throw new Error('EDGE_CORE_TOKEN is required before the edge hands out Circle user tokens')
  }
  const router = Router()

  router.use((req: Request, res: Response, next: NextFunction) => {
    if (!sameSecret(req.header('X-Misthos-Core-Token') ?? '', coreToken)) {
      res.status(401).json({ error: 'core token required' })
      return
    }
    next()
  })

  const party = (req: Request, res: Response): Party | null => {
    const value = req.params.party as Party
    if (!PARTIES.includes(value)) {
      res.status(400).json({ error: `unknown party ${value}`, allowed: PARTIES })
      return null
    }
    return value
  }

  const run =
    (op: (p: Party, id: string) => Promise<unknown>) => async (req: Request, res: Response) => {
      const p = party(req, res)
      if (p === null) return
      try {
        res.json(await op(p, String(req.params.id)))
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err)
        const status = message.startsWith('invalid ') ? 400 : 502
        res.status(status).json({ error: message })
      }
    }

  const signIn =
    (op: (id: string, body: { message?: unknown }) => Promise<unknown>) =>
    async (req: Request, res: Response) => {
      try {
        res.json(await op(String(req.params.id), (req.body ?? {}) as { message?: unknown }))
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err)
        const status = message.startsWith('invalid ') ? 400 : 502
        res.status(status).json({ error: message })
      }
    }

  // A Circle wallet as the way in (#181): keyed by the browser's sign-in id, before any
  // party exists. Registered before the party routes, which would take `sign-in` for one.
  router.post('/sign-in/:id/session', signIn((id) => service.signInSession(id)))
  router.get('/sign-in/:id', signIn((id) => service.signInWallet(id)))
  router.post(
    '/sign-in/:id/sign',
    signIn((id, body) => {
      if (typeof body.message !== 'string' || body.message.length > 4000) {
        throw new Error('invalid message: send the sign-in message as text')
      }
      return service.signInSignature(id, body.message)
    }),
  )
  router.post('/:party/:id/session', run((p, id) => service.session(p, id)))
  router.get('/:party/:id', run((p, id) => service.wallet(p, id)))
  return router
}

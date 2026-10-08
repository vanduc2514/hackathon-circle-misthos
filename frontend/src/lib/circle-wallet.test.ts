import { describe, expect, it } from 'vitest'
import { setUpWallet, type ChallengeRunner, type WalletSession } from './circle-wallet'

const LIVE: WalletSession & { wallet: unknown } = {
  app_id: 'app-1',
  user_token: 'ut',
  encryption_key: 'ek',
  challenge_id: 'challenge-1',
  simulated: false,
  wallet: null,
}

function fakeFrame(fail?: Error) {
  const ran: string[] = []
  const runner: ChallengeRunner = {
    run: async (session) => {
      ran.push(session.challenge_id)
      if (fail) throw fail
    },
  }
  return { runner, ran }
}

describe('setUpWallet', () => {
  it('runs the PIN challenge, then has the API read the new address back', async () => {
    const { runner, ran } = fakeFrame()
    let linked = 0
    const outcome = await setUpWallet(async () => LIVE, async () => (linked += 1), runner)

    expect(outcome).toBe('created')
    expect(ran).toEqual(['challenge-1'])
    expect(linked).toBe(1)
  })

  it('opens no frame when the wallet already exists; the session linked it', async () => {
    const { runner, ran } = fakeFrame()
    const existing = { ...LIVE, challenge_id: null, wallet: { address: '0xB0B' } }
    expect(await setUpWallet(async () => existing, async () => {}, runner)).toBe('linked')
    expect(ran).toEqual([])
  })

  it('opens no frame in the simulation', async () => {
    const { runner, ran } = fakeFrame()
    const simulated = { ...LIVE, simulated: true, challenge_id: null, wallet: { address: '0x1' } }
    expect(await setUpWallet(async () => simulated, async () => {}, runner)).toBe('linked')
    expect(ran).toEqual([])
  })

  it('links nothing when the person does not finish the challenge', async () => {
    const { runner } = fakeFrame(new Error('The wallet set-up was not completed'))
    let linked = 0
    await expect(setUpWallet(async () => LIVE, async () => (linked += 1), runner)).rejects.toThrow(
      /not completed/,
    )
    expect(linked).toBe(0)
  })
})

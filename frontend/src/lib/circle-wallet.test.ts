import { describe, expect, it } from 'vitest'
import { circleSigner, type ChallengeRunner, type CircleApi, type CircleSession } from './circle-wallet'

const ADDRESS = '0x6ac2b2e73846a141f2cb8885b54e777ccb5a1b7d'
const FRAME = { app_id: 'app-1', user_token: 'ut', encryption_key: 'ek' }

/** The API's Circle routes: a wallet that exists once its creation challenge has run. */
function fakeApi(opts: { exists?: boolean } = {}) {
  let created = Boolean(opts.exists)
  const signed: string[] = []
  const sessions: string[] = []
  const api: CircleApi = {
    session: async (): Promise<CircleSession> => {
      sessions.push('session')
      return created
        ? { ...FRAME, challenge_id: null, address: ADDRESS }
        : { ...FRAME, challenge_id: 'create-1', address: null }
    },
    wallet: async () => ({ address: created ? ADDRESS : null }),
    sign: async (message) => {
      signed.push(message)
      return { ...FRAME, challenge_id: 'sign-1', signature: null }
    },
  }
  return { api, signed, sessions, create: () => (created = true) }
}

function fakeFrame(onRun?: (challenge: string) => void, fail?: Error) {
  const ran: string[] = []
  const runner: ChallengeRunner = {
    run: async ({ challenge_id }) => {
      ran.push(challenge_id)
      onRun?.(challenge_id)
      if (fail) throw fail
      return challenge_id.startsWith('sign') ? { signature: '0xfromtheframe' } : {}
    },
  }
  return { runner, ran }
}

describe('circleSigner', () => {
  it('creates the wallet with a PIN first, then signs in as its address', async () => {
    const { api, create, sessions } = fakeApi()
    const { runner, ran } = fakeFrame((c) => c === 'create-1' && create())

    const signer = await circleSigner(api, runner)

    expect(ran).toEqual(['create-1'])
    expect(sessions).toEqual(['session']) // read back, not started again
    expect(signer.kind).toBe('circle')
    expect(signer.address.toLowerCase()).toBe(ADDRESS)
    expect(signer.address).not.toBe(ADDRESS) // EIP-55, as the sign-in message names it
  })

  it('signs the sign-in message in Circle’s frame, with the PIN', async () => {
    const { api, signed } = fakeApi({ exists: true })
    const { runner, ran } = fakeFrame()

    const signer = await circleSigner(api, runner)
    const signature = await signer.sign('the sign-in message')

    expect(signed).toEqual(['the sign-in message'])
    expect(ran).toEqual(['sign-1'])
    expect(signature).toBe('0xfromtheframe')
  })


  it('signs in as nobody when the owner does not finish creating the wallet', async () => {
    const { api } = fakeApi()
    const { runner } = fakeFrame(undefined, new Error('Circle did not finish'))
    await expect(circleSigner(api, runner)).rejects.toThrow(/did not finish/)
  })

  it('waits for Circle to finish creating the wallet after the frame closes', async () => {
    const { api, create } = fakeApi()
    let asked = 0
    const slow: CircleApi = {
      ...api,
      wallet: async () => {
        asked += 1
        if (asked === 3) create() // the wallet appears on the third look
        return api.wallet()
      },
    }
    const { runner } = fakeFrame()

    const signer = await circleSigner(slow, runner, 0)

    expect(signer.address.toLowerCase()).toBe(ADDRESS)
  })

  it('says so when the wallet still does not exist after the frame closed', async () => {
    const { api } = fakeApi()
    const { runner } = fakeFrame()
    await expect(circleSigner(api, runner, 0)).rejects.toThrow(/not created your wallet/)
  })
})

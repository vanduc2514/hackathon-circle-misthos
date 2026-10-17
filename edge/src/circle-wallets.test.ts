import assert from 'node:assert/strict'
import type { AddressInfo } from 'node:net'
import { describe, it } from 'node:test'
import express from 'express'
import {
  ARC_TESTNET,
  circleUserId,
  createWalletService,
  signInUserId,
  walletConfig,
  type CircleUsers,
  type WalletConfig,
} from './circle-wallets.js'
import { walletRouter } from './wallet-routes.js'

const LIVE: WalletConfig = {
  apiKey: 'TEST_API_KEY:x:y',
  appId: 'app-1',
  blockchain: ARC_TESTNET,
}

const CREDENTIALS = { CIRCLE_API_KEY: 'TEST_API_KEY:x:y', CIRCLE_APP_ID: 'app-1' }

/** A stand-in for Circle that records calls and holds wallets in memory. */
function fakeCircle(existing: Record<string, string> = {}) {
  const calls: string[] = []
  const users = new Set(Object.keys(existing))
  const circle: CircleUsers = {
    async createUser({ userId }) {
      calls.push(`createUser ${userId}`)
      if (users.has(userId)) throw Object.assign(new Error('exists'), { code: 155101 })
      users.add(userId)
      return {}
    },
    async createUserToken({ userId }) {
      calls.push(`createUserToken ${userId}`)
      return { data: { userToken: `token-${userId}`, encryptionKey: 'key' } }
    },
    async createUserPinWithWallets(input) {
      calls.push(`createUserPinWithWallets ${input.blockchains.join()} ${input.accountType}`)
      return { data: { challengeId: 'challenge-1' } }
    },
    async listWallets({ userToken }) {
      const userId = userToken.replace('token-', '')
      const address = existing[userId]
      return { data: { wallets: address ? [{ address, blockchain: ARC_TESTNET }] : [] } }
    },
    async signMessage(input) {
      calls.push(`signMessage ${input.walletAddress} ${input.blockchain} ${input.encodedByHex}`)
      return { data: { challengeId: 'sign-challenge-1' } }
    },
  }
  return { circle, calls }
}

const SIGN_IN = 'browser-7f3a9c2e5b1d4e8f'
const SIGN_IN_USER = `misthos-signin-${SIGN_IN}`

/** A sign-in message as the web app builds it, naming `address` on its second line. */
function signInMessage(address: string): string {
  return [
    'localhost:5173 wants you to sign in with your Ethereum account:',
    address,
    '',
    'Sign in to Misthos. This costs nothing and moves no money.',
    '',
    'URI: http://localhost:5173',
    'Version: 1',
    'Chain ID: 5042002',
    'Nonce: 0123456789abcdef01234567',
    'Issued At: 2026-10-16T00:00:00.000Z',
  ].join('\n')
}

describe('wallet configuration', () => {
  it('is on Arc testnet by default and needs Circle credentials', () => {
    assert.throws(() => walletConfig({}), /CIRCLE_API_KEY/)
    assert.equal(walletConfig({ ...CREDENTIALS }).blockchain, ARC_TESTNET)
  })

  it('refuses mainnet wallets until the confirmation names chain 5042', () => {
    const env = {
      CIRCLE_API_KEY: 'k',
      CIRCLE_APP_ID: 'a',
      CIRCLE_WALLET_BLOCKCHAIN: 'ARC',
    }
    assert.throws(() => walletConfig(env), /EDGE_CONFIRM_MAINNET/)
    assert.equal(walletConfig({ ...env, EDGE_CONFIRM_MAINNET: '5042' }).blockchain, 'ARC')
  })

  it('namespaces ids and refuses anything that is not a plain id', () => {
    assert.equal(circleUserId('contributor', 'CON-1'), 'misthos-contributor-CON-1')
    assert.throws(() => circleUserId('publisher', '../admin'), /invalid/)
  })
})

describe('live wallet service', () => {
  it('creates the user and a PIN challenge for a smart account on Arc', async () => {
    const { circle, calls } = fakeCircle()
    const session = await createWalletService(LIVE, circle).session('contributor', 'CON-1')

    assert.equal(session.challengeId, 'challenge-1')
    assert.equal(session.address, null)
    assert.equal(session.appId, 'app-1')
    assert.ok(calls.includes(`createUserPinWithWallets ${ARC_TESTNET} SCA`))
  })

  it('reuses an existing user and wallet instead of asking for another', async () => {
    const { circle, calls } = fakeCircle({ 'misthos-contributor-CON-1': '0xC0FE' })
    const session = await createWalletService(LIVE, circle).session('contributor', 'CON-1')

    assert.equal(session.challengeId, null)
    assert.equal(session.address, '0xC0FE')
    assert.ok(!calls.some((c) => c.startsWith('createUserPinWithWallets')))
  })

  it('reads the Arc address back once the user has created the wallet', async () => {
    const { circle } = fakeCircle({ 'misthos-publisher-PUB-1': '0xB0B' })
    const wallet = await createWalletService(LIVE, circle).wallet('publisher', 'PUB-1')
    assert.equal(wallet.address, '0xB0B')
  })

  it('passes on any Circle failure other than an existing user', async () => {
    const { circle } = fakeCircle()
    circle.createUser = async () => {
      throw Object.assign(new Error('denied'), { code: 155102 })
    }
    await assert.rejects(createWalletService(LIVE, circle).session('publisher', 'PUB-1'), /denied/)
  })
})

describe('signing in with a Circle wallet', () => {
  it('keys the Circle user by the sign-in id and refuses an id too short to be random', () => {
    assert.equal(signInUserId(SIGN_IN), SIGN_IN_USER)
    assert.throws(() => signInUserId('short'), /invalid/)
    assert.throws(() => signInUserId('../../admin/please'), /invalid/)
  })

  // An EOA, because the API recovers the address from the signature; a smart account
  // signs with EIP-1271, which cannot be recovered.
  it('creates an EOA for a new sign-in, not a smart account', async () => {
    const { circle, calls } = fakeCircle()
    const session = await createWalletService(LIVE, circle).signInSession(SIGN_IN)

    assert.equal(session.challengeId, 'challenge-1')
    assert.equal(session.circleUserId, SIGN_IN_USER)
    assert.ok(calls.includes(`createUserPinWithWallets ${ARC_TESTNET} EOA`))
  })

  it("asks Circle to sign the sign-in message with the user's own wallet", async () => {
    const { circle, calls } = fakeCircle({ [SIGN_IN_USER]: '0xc1c1' })
    const signed = await createWalletService(LIVE, circle).signInSignature(
      SIGN_IN,
      signInMessage('0xC1c1'),
    )

    assert.equal(signed.challengeId, 'sign-challenge-1')
    assert.equal(signed.signature, null)
    assert.ok(calls.includes(`signMessage 0xc1c1 ${ARC_TESTNET} false`))
  })

  it('reads the wallet back without asking Circle for anything new', async () => {
    const { circle, calls } = fakeCircle({ [SIGN_IN_USER]: '0xc1c1' })
    const wallet = await createWalletService(LIVE, circle).signInWallet(SIGN_IN)
    const none = fakeCircle()
    const notYet = await createWalletService(LIVE, none.circle).signInWallet(SIGN_IN)

    assert.equal(wallet.address, '0xc1c1')
    assert.equal(notYet.address, null)
    for (const made of [...calls, ...none.calls]) {
      assert.ok(!/^(createUser |createUserPinWithWallets|signMessage)/.test(made), made)
    }
  })

  it('reads a sign-in message written with CRLF line ends as the API does', async () => {
    const { circle } = fakeCircle({ [SIGN_IN_USER]: '0xc1c1' })
    const crlf = signInMessage('0xC1c1').replace(/\n/g, '\r\n')
    const signed = await createWalletService(LIVE, circle).signInSignature(SIGN_IN, crlf)
    assert.equal(signed.challengeId, 'sign-challenge-1')
  })

  it('puts no other message in front of the wallet, and none before it exists', async () => {
    const { circle, calls } = fakeCircle({ [SIGN_IN_USER]: '0xc1c1' })
    const service = createWalletService(LIVE, circle)
    await assert.rejects(service.signInSignature(SIGN_IN, signInMessage('0xd00d')), /another wallet/)
    await assert.rejects(service.signInSignature(SIGN_IN, 'send all my USDC'), /another wallet/)
    const none = fakeCircle()
    await assert.rejects(
      createWalletService(LIVE, none.circle).signInSignature(SIGN_IN, signInMessage('0xc1c1')),
      /not created yet/,
    )
    assert.ok(!calls.some((c) => c.startsWith('signMessage')))
  })

})

describe('wallet routes', () => {
  async function serve(router: express.Router) {
    const app = express()
    app.use(express.json()) // as index.ts parses every body
    app.use('/wallets', router)
    const server = app.listen(0)
    await new Promise((resolve) => server.once('listening', resolve))
    const { port } = server.address() as AddressInfo
    return {
      base: `http://127.0.0.1:${port}/wallets`,
      close: () =>
        new Promise((resolve) => {
          server.closeAllConnections()
          server.close(resolve)
        }),
    }
  }

  it('will not hand out user tokens without a core token configured', () => {
    const { circle } = fakeCircle()
    assert.throws(() => walletRouter(createWalletService(LIVE, circle), ''), /EDGE_CORE_TOKEN/)
  })

  it('answers the core and refuses anyone else', async () => {
    const { circle } = fakeCircle()
    const app = await serve(walletRouter(createWalletService(LIVE, circle), 'secret'))
    const stranger = await fetch(`${app.base}/contributor/CON-1/session`, { method: 'POST' })
    const core = await fetch(`${app.base}/contributor/CON-1/session`, {
      method: 'POST',
      headers: { 'X-Misthos-Core-Token': 'secret' },
    })
    const body = (await core.json()) as { challengeId: string }
    await app.close()

    assert.equal(stranger.status, 401)
    assert.equal(core.status, 200)
    assert.equal(body.challengeId, 'challenge-1')
  })

  // Built from the environment, as index.ts builds them: whichever way the flag says
  // live, the routes are Circle's and the core token guards them.
  it('asks for the core token whenever the flag is live, however it is spelled', async () => {
    for (const value of ['0', 'no', 'off']) {
      const { circle } = fakeCircle()
      const service = createWalletService(
        walletConfig({ MISTHOS_SIMULATED: value, ...CREDENTIALS }),
        circle,
      )
      assert.throws(() => walletRouter(service, ''), /EDGE_CORE_TOKEN/, value)

      const app = await serve(walletRouter(service, 'secret'))
      const session = await fetch(`${app.base}/contributor/CON-1/session`, { method: 'POST' })
      const wallet = await fetch(`${app.base}/contributor/CON-1`)
      const wrong = await fetch(`${app.base}/contributor/CON-1`, {
        headers: { 'X-Misthos-Core-Token': 'guess' },
      })
      const core = await fetch(`${app.base}/contributor/CON-1/session`, {
        method: 'POST',
        headers: { 'X-Misthos-Core-Token': 'secret' },
      })
      const body = (await core.json()) as { challengeId: string }
      await app.close()

      assert.equal(session.status, 401, value)
      assert.equal(wallet.status, 401, value)
      assert.equal(wrong.status, 401, value)
      assert.equal(core.status, 200, value)
      assert.equal(body.challengeId, 'challenge-1', value)
    }
  })

  it('serves a sign-in to the core, and says what is wrong with a message it will not sign', async () => {
    const { circle } = fakeCircle({ [SIGN_IN_USER]: '0xc1c1' })
    const app = await serve(walletRouter(createWalletService(LIVE, circle), 'secret'))
    const post = (path: string, body?: unknown, token = 'secret') =>
      fetch(`${app.base}${path}`, {
        method: 'POST',
        headers: { 'X-Misthos-Core-Token': token, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
    const stranger = await post(`/sign-in/${SIGN_IN}/session`, undefined, 'guess')
    const session = await post(`/sign-in/${SIGN_IN}/session`)
    const read = await fetch(`${app.base}/sign-in/${SIGN_IN}`, {
      headers: { 'X-Misthos-Core-Token': 'secret' },
    })
    const signed = await post(`/sign-in/${SIGN_IN}/sign`, { message: signInMessage('0xc1c1') })
    const refused = await post(`/sign-in/${SIGN_IN}/sign`, { message: 42 })
    const badId = await post('/sign-in/short/session')
    const sessionBody = (await session.json()) as { address: string }
    const signedBody = (await signed.json()) as { challengeId: string }
    await app.close()

    assert.equal(stranger.status, 401)
    assert.equal(session.status, 200)
    assert.equal(read.status, 200)
    assert.equal(sessionBody.address, '0xc1c1')
    assert.equal(signed.status, 200)
    assert.equal(signedBody.challengeId, 'sign-challenge-1')
    assert.equal(refused.status, 400)
    assert.equal(badId.status, 400)
  })

  it('rejects a party it does not know', async () => {
    const { circle } = fakeCircle()
    const app = await serve(walletRouter(createWalletService(LIVE, circle), 'secret'))
    const res = await fetch(`${app.base}/admin/1`, {
      headers: { 'X-Misthos-Core-Token': 'secret' },
    })
    await app.close()
    assert.ok([400, 404].includes(res.status), String(res.status))
  })
})

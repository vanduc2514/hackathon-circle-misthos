import assert from 'node:assert/strict'
import type { AddressInfo } from 'node:net'
import { describe, it } from 'node:test'
import express from 'express'
import {
  ARC_TESTNET,
  circleUserId,
  createWalletService,
  walletConfig,
  type CircleUsers,
  type WalletConfig,
} from './circle-wallets.js'
import { walletRouter } from './wallet-routes.js'

const LIVE: WalletConfig = {
  simulated: false,
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
  }
  return { circle, calls }
}

describe('wallet configuration', () => {
  it('is simulated and on Arc testnet unless told otherwise', () => {
    const config = walletConfig({})
    assert.equal(config.simulated, true)
    assert.equal(config.blockchain, ARC_TESTNET)
  })

  it('refuses real wallets without Circle credentials', () => {
    assert.throws(() => walletConfig({ MISTHOS_SIMULATED: 'false' }), /CIRCLE_API_KEY/)
  })

  // The API reads `0`, `no` and `off` as live. Read as simulated here, they gave the
  // live API made-up addresses to keep as payout wallets.
  it('is live for every spelling the API reads as live, and then needs Circle', () => {
    for (const value of ['0', 'f', 'n', 'no', 'off', 'False', 'OFF']) {
      assert.equal(walletConfig({ MISTHOS_SIMULATED: value, ...CREDENTIALS }).simulated, false, value)
      assert.throws(() => walletConfig({ MISTHOS_SIMULATED: value }), /CIRCLE_API_KEY/, value)
    }
  })

  it('refuses a MISTHOS_SIMULATED the API would refuse', () => {
    for (const value of ['', 'maybe', ' false']) {
      assert.throws(() => walletConfig({ MISTHOS_SIMULATED: value }), /MISTHOS_SIMULATED/, value)
    }
  })

  it('refuses mainnet wallets until the confirmation names chain 5042', () => {
    const env = {
      MISTHOS_SIMULATED: 'false',
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

describe('simulated wallet service', () => {
  it('gives each party a stable address and never a challenge', async () => {
    const service = createWalletService(walletConfig({}))
    const first = await service.session('contributor', 'CON-1')
    const again = await service.wallet('contributor', 'CON-1')
    const other = await service.wallet('contributor', 'CON-2')

    assert.equal(first.challengeId, null)
    assert.match(first.address ?? '', /^0x[0-9a-f]{40}$/)
    assert.equal(first.address, again.address)
    assert.notEqual(first.address, other.address)
  })
})

describe('wallet routes', () => {
  async function serve(router: express.Router) {
    const app = express()
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
      const body = (await core.json()) as { simulated: boolean; challengeId: string }
      await app.close()

      assert.equal(session.status, 401, value)
      assert.equal(wallet.status, 401, value)
      assert.equal(wrong.status, 401, value)
      assert.equal(core.status, 200, value)
      assert.equal(body.simulated, false, value)
      assert.equal(body.challengeId, 'challenge-1', value)
    }
  })

  it('rejects a party it does not know', async () => {
    const app = await serve(walletRouter(createWalletService(walletConfig({}))))
    const res = await fetch(`${app.base}/admin/1`)
    await app.close()
    assert.equal(res.status, 400)
  })
})

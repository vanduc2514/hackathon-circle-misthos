import assert from 'node:assert/strict'
import type { AddressInfo } from 'node:net'
import { describe, it } from 'node:test'
import express, { type Request, type Response } from 'express'
import {
  ARC_TESTNET,
  MAINNET_FACILITATOR,
  TESTNET_FACILITATOR,
  createGate,
  gateConfig,
  settlementOf,
  type GateConfig,
} from './x402-gate.js'

const SELLER = '0x1111111111111111111111111111111111111111'

async function serve(gate: ReturnType<typeof createGate>) {
  const app = express()
  app.get('/paid', gate.require('0.25'), (_req: Request, res: Response) => {
    res.json(settlementOf(res))
  })
  const server = app.listen(0)
  await new Promise((resolve) => server.once('listening', resolve))
  const { port } = server.address() as AddressInfo
  return {
    url: `http://127.0.0.1:${port}/paid`,
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections()
        server.close(resolve)
      }),
  }
}

describe('gate configuration', () => {
  it('stays simulated unless told otherwise', () => {
    assert.equal(gateConfig({}).simulated, true)
  })

  // The API's `simulated` is a pydantic bool, so the two services have to read one
  // variable the same way (simulated.test.ts has every spelling). `0` used to mean live
  // to the API and simulated here, which handed out metered endpoints free to anyone
  // who sent a header.
  it('turns the simulation off for a spelling pydantic also reads as false', () => {
    const config = gateConfig({ MISTHOS_SIMULATED: '0', MISTHOS_SELLER_ADDRESS: SELLER })
    assert.equal(config.simulated, false)
  })

  it('defaults to the testnet facilitator, because the SDK defaults to mainnet', () => {
    const config = gateConfig({ MISTHOS_SIMULATED: 'false', MISTHOS_SELLER_ADDRESS: SELLER })
    assert.equal(config.facilitatorUrl, TESTNET_FACILITATOR)
    assert.deepEqual(config.networks, [ARC_TESTNET])
  })

  it('refuses to take real payments without a seller address', () => {
    assert.throws(() => gateConfig({ MISTHOS_SIMULATED: 'false' }), /MISTHOS_SELLER_ADDRESS/)
  })

  it('refuses mainnet until the confirmation names chain 5042', () => {
    const env = {
      MISTHOS_SIMULATED: 'false',
      MISTHOS_SELLER_ADDRESS: SELLER,
      GATEWAY_FACILITATOR_URL: MAINNET_FACILITATOR,
    }
    assert.throws(() => gateConfig(env), /EDGE_CONFIRM_MAINNET/)
    assert.throws(() => gateConfig({ ...env, EDGE_CONFIRM_MAINNET: 'true' }), /EDGE_CONFIRM_MAINNET/)
    assert.equal(gateConfig({ ...env, EDGE_CONFIRM_MAINNET: '5042' }).simulated, false)
  })
})

describe('simulated gate', () => {
  const config = gateConfig({})

  it('answers an unpaid request with 402 and a price in 6-decimal USDC', async () => {
    const app = await serve(createGate(config))
    const res = await fetch(app.url)
    const body = (await res.json()) as { accepts: { maxAmountRequired: string; network: string }[] }
    await app.close()

    assert.equal(res.status, 402)
    assert.equal(body.accepts[0].maxAmountRequired, '250000')
    assert.equal(body.accepts[0].network, ARC_TESTNET)
  })

  it('lets a paid request through and says the settlement was simulated', async () => {
    const app = await serve(createGate(config))
    const res = await fetch(app.url, { headers: { 'X-PAYMENT': 'proof' } })
    const body = (await res.json()) as { rail: string }
    await app.close()

    assert.equal(res.status, 200)
    assert.equal(body.rail, 'simulated')
  })
})

describe('live gate', () => {
  const live: GateConfig = {
    simulated: false,
    sellerAddress: SELLER,
    facilitatorUrl: TESTNET_FACILITATOR,
    networks: [ARC_TESTNET],
  }

  it('hands the Gateway middleware the seller, network and facilitator', () => {
    let seen: unknown
    createGate(live, (cfg) => {
      seen = cfg
      return { require: () => () => undefined }
    })
    assert.deepEqual(seen, {
      sellerAddress: SELLER,
      networks: [ARC_TESTNET],
      facilitatorUrl: TESTNET_FACILITATOR,
      description: 'Misthos',
    })
  })

  it('charges the price in dollars and passes on what the facilitator settled', async () => {
    let price = ''
    const gate = createGate(live, () => ({
      require: (p) => {
        price = p
        return (req, _res, next) => {
          Object.assign(req, {
            payment: {
              verified: true,
              payer: '0xBUYER',
              amount: '250000',
              network: ARC_TESTNET,
              transaction: '0xTX',
            },
          })
          next()
        }
      },
    }))
    const app = await serve(gate)
    const body = (await (await fetch(app.url)).json()) as Record<string, string>
    await app.close()

    assert.equal(price, '$0.25')
    assert.deepEqual(body, {
      payer: '0xBUYER',
      network: ARC_TESTNET,
      amount: '250000',
      transaction: '0xTX',
      rail: 'gateway-nanopayments',
    })
  })

  it('never reaches the handler when the middleware does not settle', async () => {
    const gate = createGate(live, () => ({
      require: () => (_req, res) => {
        res.statusCode = 402
        res.end('{"error":"payment required"}')
      },
    }))
    const app = await serve(gate)
    const res = await fetch(app.url)
    await app.close()
    assert.equal(res.status, 402)
  })
})

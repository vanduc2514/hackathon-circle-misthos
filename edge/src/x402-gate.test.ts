import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { ARC_USDC, BUYER, GATEWAY_WALLET, LIVE, SELLER, fakeFacilitator, signed } from './test-fakes.js'
import {
  ARC_TESTNET,
  AUTHORISATION_WINDOW_SECONDS,
  MAINNET_FACILITATOR,
  TESTNET_FACILITATOR,
  baseUnits,
  createRail,
  gateConfig,
  paymentRequiredHeader,
  paymentResponseHeader,
} from './x402-gate.js'

const decode = (header: string) => JSON.parse(Buffer.from(header, 'base64').toString('utf-8'))

describe('gate configuration', () => {
  it('defaults to the testnet facilitator, because the SDK defaults to mainnet', () => {
    const config = gateConfig({ MISTHOS_SELLER_ADDRESS: SELLER })
    assert.equal(config.facilitatorUrl, TESTNET_FACILITATOR)
    assert.deepEqual(config.networks, [ARC_TESTNET])
  })

  it('refuses to take payments without a seller address', () => {
    assert.throws(() => gateConfig({}), /MISTHOS_SELLER_ADDRESS/)
  })

  it('refuses mainnet until the confirmation names chain 5042', () => {
    const env = {
      MISTHOS_SELLER_ADDRESS: SELLER,
      GATEWAY_FACILITATOR_URL: MAINNET_FACILITATOR,
    }
    assert.throws(() => gateConfig(env), /EDGE_CONFIRM_MAINNET/)
    assert.throws(() => gateConfig({ ...env, EDGE_CONFIRM_MAINNET: 'true' }), /EDGE_CONFIRM_MAINNET/)
    assert.equal(gateConfig({ ...env, EDGE_CONFIRM_MAINNET: '5042' }).networks[0], ARC_TESTNET)
  })
})

describe('prices in base units', () => {
  it('turns USDC decimal text into six-decimal base units without floating point', () => {
    assert.equal(baseUnits('0.01'), '10000')
    assert.equal(baseUnits('0.25'), '250000')
    assert.equal(baseUnits('1'), '1000000')
    assert.equal(baseUnits('0.000001'), '1')
  })

  it('refuses a price that is zero, negative, too precise or not a number', () => {
    for (const bad of ['0', '0.0', '-1', '0.0000001', '1e3', '', 'free', '$0.25']) {
      assert.throws(() => baseUnits(bad), /USDC amount|more than zero/, bad)
    }
  })
})

describe('the rail', () => {
  it('names Arc testnet, its USDC, the seller and the amount, and nothing it was not told to accept', async () => {
    const { facilitator } = fakeFacilitator()
    const accepts = await createRail(LIVE, facilitator).requirements('0.05')
    assert.deepEqual(accepts, [
      {
        scheme: 'exact',
        network: ARC_TESTNET,
        asset: ARC_USDC,
        amount: '50000',
        payTo: SELLER,
        maxTimeoutSeconds: AUTHORISATION_WINDOW_SECONDS,
        extra: { name: 'GatewayWalletBatched', version: '1', verifyingContract: GATEWAY_WALLET },
      },
    ])
  })

  it('asks the facilitator what it supports once, not on every call', async () => {
    const { facilitator, calls } = fakeFacilitator()
    const rail = createRail(LIVE, facilitator)
    await rail.requirements('0.01')
    await rail.requirements('0.02')
    await rail.verify(signed('10000'), '0.01')
    assert.equal(calls.supported, 1)
  })

  it('verifies against our price, so a payment signed for less is refused', async () => {
    const { facilitator, calls } = fakeFacilitator()
    const rail = createRail(LIVE, facilitator)
    const low = await rail.verify(signed('10000'), '0.05')
    assert.deepEqual(low, { ok: false, status: 402, reason: 'insufficient_amount' })
    assert.equal(calls.verified[0].amount, '50000')
    assert.equal(calls.settled.length, 0)
  })

  it('refuses a payment that is not base64 JSON, or names a network we do not take', async () => {
    const { facilitator } = fakeFacilitator()
    const rail = createRail(LIVE, facilitator)
    assert.equal((await rail.verify('%%%', '0.01')).ok, false)
    const elsewhere = await rail.verify(signed('10000', { network: 'eip155:84532' }), '0.01')
    assert.deepEqual(elsewhere, { ok: false, status: 400, reason: 'network eip155:84532 is not accepted' })
  })

  it('settles a verified payment once and reports what moved', async () => {
    const { facilitator } = fakeFacilitator()
    const rail = createRail(LIVE, facilitator)
    const checked = await rail.verify(signed('10000', { nonce: 'n-7' }), '0.01')
    assert.ok(checked.ok)
    const settled = await rail.settle(checked.verified)
    assert.deepEqual(settled, {
      ok: true,
      settlement: {
        payer: BUYER,
        network: ARC_TESTNET,
        amount: '10000',
        asset: ARC_USDC,
        transaction: '0xbatch-n-7',
        rail: 'gateway-nanopayments',
      },
    })
    assert.deepEqual(await rail.settle(checked.verified), { ok: false, reason: 'nonce_used' })
  })

  it('reports a facilitator that throws as a failure, not a crash', async () => {
    const { facilitator } = fakeFacilitator()
    const rail = createRail(LIVE, {
      ...facilitator,
      settle: async () => {
        throw new Error('gateway down')
      },
    })
    const checked = await rail.verify(signed('10000'), '0.01')
    assert.ok(checked.ok)
    assert.deepEqual(await rail.settle(checked.verified), { ok: false, reason: 'gateway down', ambiguous: true })
  })
})

describe('the x402 headers', () => {
  it('PAYMENT-REQUIRED is base64 JSON, version 2, with the resource and what we accept', async () => {
    const { facilitator } = fakeFacilitator()
    const accepts = await createRail(LIVE, facilitator).requirements('0.01')
    const header = decode(paymentRequiredHeader('/paid/x?page=2', 'A paid thing', accepts))
    assert.equal(header.x402Version, 2)
    assert.deepEqual(header.resource, { url: '/paid/x?page=2', description: 'A paid thing', mimeType: 'application/json' })
    assert.deepEqual(header.accepts, accepts)
  })

  it('PAYMENT-RESPONSE carries the transaction, network and payer', () => {
    const header = decode(
      paymentResponseHeader({
        payer: BUYER,
        network: ARC_TESTNET,
        amount: '10000',
        asset: ARC_USDC,
        transaction: '0xTX',
        rail: 'gateway-nanopayments',
      }),
    )
    assert.deepEqual(header, { success: true, transaction: '0xTX', network: ARC_TESTNET, payer: BUYER })
  })
})

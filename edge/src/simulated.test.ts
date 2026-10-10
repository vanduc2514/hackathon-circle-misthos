import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { walletConfig } from './circle-wallets.js'
import { simulatedFrom } from './simulated.js'
import { gateConfig } from './x402-gate.js'

// Every spelling the backend's `Settings.simulated` reads, checked against pydantic
// 2.13 (pydantic-core 2.46): the six words each way, in any ASCII case, and nothing
// else. Pydantic trims nothing, so neither does the edge.
const SIMULATED = ['1', 'true', 't', 'yes', 'y', 'on']
  .flatMap((v) => [v, v.toUpperCase(), v[0].toUpperCase() + v.slice(1)])
  .concat(['tRuE', 'oN'])
const LIVE = ['0', 'false', 'f', 'no', 'n', 'off']
  .flatMap((v) => [v, v.toUpperCase(), v[0].toUpperCase() + v.slice(1)])
  .concat(['fAlSe', 'oFf'])
const REFUSED = [
  '',
  ' ',
  ' On ',
  ' 0',
  '0 ',
  'off\n',
  '\ttrue',
  '2',
  '-1',
  '+1',
  '00',
  '01',
  '1.0',
  '0.0',
  'maybe',
  'simulated',
  'live',
  'none',
  'null',
  '\uff54rue', // a fullwidth t
  'tru',
  'yess',
  '\u0130', // a dotted capital I, which JavaScript lowercases to two characters
]

const SELLER = '0x1111111111111111111111111111111111111111'
const CONFIGURED = { MISTHOS_SELLER_ADDRESS: SELLER, CIRCLE_API_KEY: 'k', CIRCLE_APP_ID: 'a' }

describe('MISTHOS_SIMULATED', () => {
  it('is simulated when unset, as the API is', () => {
    assert.equal(simulatedFrom(undefined), true)
  })

  it('reads every spelling the API reads as true as the simulation', () => {
    for (const value of SIMULATED) assert.equal(simulatedFrom(value), true, value)
  })

  it('reads every spelling the API reads as false as live', () => {
    for (const value of LIVE) assert.equal(simulatedFrom(value), false, value)
  })

  // A value the API refuses stops the API; read any other way here, the edge would
  // run in a mode the API never agreed to. Whitespace included: pydantic does not trim.
  it('refuses anything the API would refuse, rather than guessing', () => {
    for (const value of REFUSED) {
      assert.throws(() => simulatedFrom(value), /MISTHOS_SIMULATED/, JSON.stringify(value))
    }
  })

  // The wallet routes used to read only the literal `false` as live, so with `0` the
  // gate and the API ran live while the wallet routes handed out made-up addresses
  // with no core token asked for, and the API kept them as payout wallets.
  it('is read the same way by the x402 gate and the wallet routes', () => {
    for (const value of [...SIMULATED, ...LIVE]) {
      const env = { MISTHOS_SIMULATED: value, ...CONFIGURED }
      assert.equal(walletConfig(env).simulated, simulatedFrom(value), value)
      assert.equal(gateConfig(env).simulated, simulatedFrom(value), value)
    }
    for (const value of REFUSED) {
      const env = { MISTHOS_SIMULATED: value, ...CONFIGURED }
      assert.throws(() => walletConfig(env), /MISTHOS_SIMULATED/, JSON.stringify(value))
      assert.throws(() => gateConfig(env), /MISTHOS_SIMULATED/, JSON.stringify(value))
    }
  })
})

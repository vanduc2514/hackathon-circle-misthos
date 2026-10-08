import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { describe, it } from 'node:test'

const ENTRY = fileURLToPath(new URL('./index.ts', import.meta.url))
const EDGE = fileURLToPath(new URL('..', import.meta.url))
const SELLER = '0x1111111111111111111111111111111111111111'
const CIRCLE = { CIRCLE_API_KEY: 'TEST_API_KEY:x:y', CIRCLE_APP_ID: 'app-1' }

/** Start the edge as `npm run dev` does, with only `env` configured. NODE_ENV=test
 * keeps it from listening, so a start that succeeds exits 0, and one that is refused
 * exits with the reason. Nothing here reaches Circle: a refused start stops before
 * any request, and the simulated one never makes any. */
function start(env: Record<string, string>): Promise<{ code: number; stderr: string }> {
  const inherited = Object.fromEntries(
    ['PATH', 'HOME', 'TMPDIR', 'SystemRoot'].flatMap((k) =>
      process.env[k] === undefined ? [] : [[k, process.env[k] as string]],
    ),
  )
  return new Promise((resolve) => {
    execFile(
      process.execPath,
      ['--import', 'tsx', ENTRY],
      { cwd: EDGE, env: { ...inherited, NODE_ENV: 'test', ...env }, timeout: 60_000 },
      (err, _stdout, stderr) => {
        const code = err === null ? 0 : typeof err.code === 'number' ? err.code : -1
        resolve({ code, stderr })
      },
    )
  })
}

describe('starting the edge', { concurrency: true }, () => {
  it('starts simulated with nothing configured', async () => {
    const run = await start({})
    assert.equal(run.code, 0, run.stderr)
  })

  // Live for the API and the gate meant live for the wallet routes only when spelled
  // `false`; `0` started an edge that handed the live API made-up payout addresses.
  it('will not start live on MISTHOS_SIMULATED=0 without the Circle credentials', async () => {
    const run = await start({ MISTHOS_SIMULATED: '0', MISTHOS_SELLER_ADDRESS: SELLER })
    assert.notEqual(run.code, 0)
    assert.match(run.stderr, /CIRCLE_API_KEY and CIRCLE_APP_ID are required/)
  })

  it('will not start live without the core token that guards the wallet routes', async () => {
    const run = await start({ MISTHOS_SIMULATED: '0', MISTHOS_SELLER_ADDRESS: SELLER, ...CIRCLE })
    assert.notEqual(run.code, 0)
    assert.match(run.stderr, /EDGE_CORE_TOKEN is required/)
  })

  it('will not start on a MISTHOS_SIMULATED the API would refuse', async () => {
    const run = await start({ MISTHOS_SIMULATED: 'maybe' })
    assert.notEqual(run.code, 0)
    assert.match(run.stderr, /MISTHOS_SIMULATED is not a boolean/)
  })
})

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
 * any request, and no request reaches Circle before it is needed. */
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
  it('refuses to start with nothing configured', async () => {
    const run = await start({})
    assert.notEqual(run.code, 0)
    assert.match(run.stderr, /MISTHOS_SELLER_ADDRESS/)
  })

  it('starts the gate without Circle and says the wallet routes are off', async () => {
    const run = await start({ MISTHOS_SELLER_ADDRESS: SELLER, EDGE_CORE_TOKEN: 'secret' })
    assert.equal(run.code, 0, run.stderr)
    assert.match(run.stderr, /Circle wallet routes are not mounted/)
  })

  it('starts live and names the removed switch once', async () => {
    const run = await start({
      MISTHOS_SIMULATED: 'maybe',
      MISTHOS_SELLER_ADDRESS: SELLER,
      EDGE_CORE_TOKEN: 'secret',
      ...CIRCLE,
    })
    assert.equal(run.code, 0, run.stderr)
    assert.match(run.stderr, /simulation has been removed/)
  })
})

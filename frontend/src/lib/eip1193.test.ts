import { afterEach, describe, expect, it } from 'vitest'
import { browserProvider, type Eip1193 } from './eip1193'

/**
 * `browserProvider` reads whatever the browser injected, so each test installs its
 * own `ethereum` and takes it away again rather than depending on the runtime.
 */
afterEach(() => {
  delete (globalThis as { ethereum?: unknown }).ethereum
})

const install = (ethereum: unknown) => {
  ;(globalThis as { ethereum?: unknown }).ethereum = ethereum
}

describe('browserProvider', () => {
  it('returns the injected wallet when the browser has one', () => {
    const injected: Eip1193 = { request: async () => null }
    install(injected)
    expect(browserProvider()).toBe(injected)
  })

  it('returns null when no wallet is installed', () => {
    expect(browserProvider()).toBeNull()
  })

  it('refuses an ethereum that cannot take requests', () => {
    // Some pages define `ethereum` for something else entirely. A wallet is only a
    // wallet if it can be asked to sign, and a call to a missing method would fail
    // later, in the middle of a sign-in, instead of here.
    install({ isMetaMask: true })
    expect(browserProvider()).toBeNull()
  })

  it('refuses a request that is present but not callable', () => {
    install({ request: 'ask nicely' })
    expect(browserProvider()).toBeNull()
  })

  it('refuses a null ethereum, which is what some extensions leave behind', () => {
    install(null)
    expect(browserProvider()).toBeNull()
  })
})

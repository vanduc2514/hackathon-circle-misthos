import { afterEach, describe, expect, it, vi } from 'vitest'
import { demoWallet, demoWalletFor } from './wallet'

function memoryStorage() {
  const kept = new Map<string, string>()
  return {
    getItem: (key: string) => kept.get(key) ?? null,
    setItem: (key: string, value: string) => void kept.set(key, value),
  }
}

describe('demoWalletFor', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("keeps one demo wallet per account, apart from the side's sign-in wallet", () => {
    vi.stubGlobal('localStorage', memoryStorage())
    const first = demoWalletFor('CON-100')
    expect(demoWalletFor('CON-100').address).toBe(first.address)
    expect(demoWalletFor('CON-101').address).not.toBe(first.address)
    // The side's sign-in key may be an account of its own, and a wallet is one account's.
    expect(demoWallet('contributor').address).not.toBe(first.address)
  })

  it('still gives a working key where storage is blocked', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
    })
    const wallet = demoWalletFor('PUB-100')
    expect(wallet.kind).toBe('demo')
    expect(await wallet.sign('hello')).toMatch(/^0x[0-9a-f]{130}$/)
  })
})

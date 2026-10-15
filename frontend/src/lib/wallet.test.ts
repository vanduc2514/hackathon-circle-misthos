import { afterEach, describe, expect, it } from 'vitest'
import { keccak_256 } from '@noble/hashes/sha3.js'
import { bytesToHex, concatBytes, hexToBytes, utf8ToBytes } from '@noble/hashes/utils.js'
import { secp256k1 } from '@noble/curves/secp256k1.js'
import { toChecksumAddress } from './siwe'
import type { Eip1193 } from './eip1193'
import {
  addressOf,
  anyWallet,
  browserWallet,
  demoWallet,
  personalDigest,
  signPersonal,
  type Signer,
} from './wallet'

/**
 * What `siwe.test.ts` does not reach: the EIP-191 prefix itself, the wallet that
 * talks to an extension, and the demo key's persistence. `signPersonal` and
 * `addressOf` are asserted there against the API's own test wallet, so they are not
 * repeated here.
 */

// The secret key 1, the same one backend/tests/siwe_wallet.py calls Wallet(1).
const ONE = new Uint8Array(32)
ONE[31] = 1
const ONE_ADDRESS = '0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf'
const ONE_KEY = bytesToHex(ONE)

const addressFromSignature = (signature: string, message: string): string => {
  const raw = hexToBytes(signature.slice(2))
  const publicKey = secp256k1.Signature.fromBytes(
    Uint8Array.of(raw[64] - 27, ...raw.slice(0, 64)),
    'recovered',
  )
    .recoverPublicKey(personalDigest(message))
    .toBytes(false)
  return toChecksumAddress('0x' + bytesToHex(keccak_256(publicKey.slice(1)).slice(-20)))
}

describe('personalDigest', () => {
  it('prefixes the message the way EIP-191 says', () => {
    const message = 'Sign in to Misthos.'
    const body = utf8ToBytes(message)
    const prefix = utf8ToBytes(`\x19Ethereum Signed Message:\n${body.length}`)
    expect(personalDigest(message)).toEqual(keccak_256(concatBytes(prefix, body)))
  })

  it('counts bytes rather than characters in the prefix', () => {
    // A wallet prefixes the byte length. Counting characters instead would disagree
    // with every wallet in existence for any message with a non-ASCII character in
    // it, which is a sign-in that fails on someone's name rather than on their code.
    const message = 'café' // four characters, five bytes
    const asCharacters = utf8ToBytes(`\x19Ethereum Signed Message:\n${message.length}`)
    expect(personalDigest(message)).not.toEqual(
      keccak_256(concatBytes(asCharacters, utf8ToBytes(message))),
    )

    const asBytes = utf8ToBytes('\x19Ethereum Signed Message:\n5')
    expect(personalDigest(message)).toEqual(keccak_256(concatBytes(asBytes, utf8ToBytes(message))))
  })
})

describe('browserWallet', () => {
  const install = (ethereum: unknown) => {
    ;(globalThis as { ethereum?: unknown }).ethereum = ethereum
  }

  afterEach(() => {
    delete (globalThis as { ethereum?: unknown }).ethereum
  })

  it('refuses when the browser has no wallet', async () => {
    await expect(browserWallet()).rejects.toThrow('No wallet extension was found')
  })

  it('refuses when the wallet shares no account', async () => {
    install({ request: async () => [] })
    await expect(browserWallet()).rejects.toThrow('did not share an account')
  })

  it('reads the account and checksums the address the wallet reports', async () => {
    const asked: string[] = []
    install({
      request: async ({ method }: { method: string }) => {
        asked.push(method)
        // A wallet may answer in any case; the page shows the checksummed form.
        return method === 'eth_requestAccounts' ? [ONE_ADDRESS.toLowerCase()] : null
      },
    })

    const signer = await browserWallet()
    expect(asked).toEqual(['eth_requestAccounts'])
    expect(signer.kind).toBe('browser')
    expect(signer.address).toBe(ONE_ADDRESS)
  })

  it('signs with personal_sign over the hex of the message, from that account', async () => {
    const calls: { method: string; params?: unknown[] }[] = []
    const wallet: Eip1193 = {
      request: async (args) => {
        calls.push(args)
        return args.method === 'eth_requestAccounts' ? [ONE_ADDRESS] : '0xsigned'
      },
    }
    install(wallet)

    const signer = await browserWallet()
    expect(await signer.sign('hello')).toBe('0xsigned')
    // A wallet signs the bytes, and the account has to be named: an extension with
    // several accounts selected would otherwise sign from whichever it prefers.
    expect(calls[1]).toEqual({
      method: 'personal_sign',
      params: ['0x' + bytesToHex(utf8ToBytes('hello')), ONE_ADDRESS],
    })
  })
})

describe('demoWallet', () => {
  const store = new Map<string, string>()

  const installStorage = () => {
    ;(globalThis as { localStorage?: unknown }).localStorage = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
    }
  }

  afterEach(() => {
    store.clear()
    delete (globalThis as { localStorage?: unknown }).localStorage
  })

  it('keeps one key per side, so the two sides are different accounts', () => {
    installStorage()

    const first = demoWallet('publisher')
    const again = demoWallet('publisher')
    const other = demoWallet('contributor')

    // Same side, same account across a reload: the demo has to be resumable.
    expect(again.address).toBe(first.address)
    expect(other.address).not.toBe(first.address)
    expect(store.get('misthos.demo-wallet.publisher')).toMatch(/^[0-9a-f]{64}$/)
    expect(store.get('misthos.demo-wallet.contributor')).not.toBe(
      store.get('misthos.demo-wallet.publisher'),
    )
  })

  it('adopts a key that is already stored rather than replacing it', async () => {
    installStorage()
    store.set('misthos.demo-wallet.publisher', ONE_KEY)

    const wallet = demoWallet('publisher')
    expect(wallet.address).toBe(ONE_ADDRESS)
    expect(store.get('misthos.demo-wallet.publisher')).toBe(ONE_KEY)
    // The key that was already there is the one that signs.
    expect(await wallet.sign('hello')).toBe(signPersonal(ONE, 'hello'))
  })

  it('replaces a stored value that is not a key', () => {
    // Anything could be in local storage: another page's value, a stale format, half
    // a write. Only a valid key is worth keeping.
    installStorage()
    store.set('misthos.demo-wallet.publisher', 'not-a-key')

    const wallet = demoWallet('publisher')
    const stored = store.get('misthos.demo-wallet.publisher')
    expect(stored).toMatch(/^[0-9a-f]{64}$/)
    expect(stored).not.toBe('not-a-key')
    expect(wallet.address).toBe(addressOf(hexToBytes(stored as string)))
  })

  it('still returns a usable signer when storage is blocked', async () => {
    // A browser with storage disabled still has to run the demo, so the key is
    // generated for this page instead of persisting across reloads.
    ;(globalThis as { localStorage?: unknown }).localStorage = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
    }

    const wallet = demoWallet('contributor')
    expect(wallet.kind).toBe('demo')
    expect(wallet.address).toMatch(/^0x[0-9a-fA-F]{40}$/)
    // And it signs for the address it claims, which is the whole point of it.
    expect(addressFromSignature(await wallet.sign('hello'), 'hello')).toBe(wallet.address)
  })
})

describe('anyWallet', () => {
  const app: Signer = { kind: 'metamask-app', address: ONE_ADDRESS, sign: async () => '0x' }
  const injected: Eip1193 = {
    request: async ({ method }) => (method === 'eth_requestAccounts' ? [ONE_ADDRESS] : null),
  }

  it('signs with the wallet in this browser when there is one, as today', async () => {
    let opened = false
    const signer = await anyWallet(
      async () => injected,
      async () => {
        opened = true
        return app
      },
    )
    expect(signer.kind).toBe('browser')
    expect(signer.address).toBe(ONE_ADDRESS)
    expect(opened).toBe(false)
  })

  it('opens the MetaMask app when the browser has no wallet, as on a phone', async () => {
    const signer = await anyWallet(async () => null, async () => app)
    expect(signer.kind).toBe('metamask-app')
  })
})

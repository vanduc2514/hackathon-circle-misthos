import { afterEach, describe, expect, it } from 'vitest'
import { keccak_256 } from '@noble/hashes/sha3.js'
import { bytesToHex, concatBytes, utf8ToBytes } from '@noble/hashes/utils.js'
import type { Eip1193 } from './eip1193'
import { browserWallet, personalDigest } from './wallet'

/**
 * What `siwe.test.ts` does not reach: the EIP-191 prefix itself, the wallet that
 * talks to an extension. `signPersonal` and
 * `addressOf` are asserted there against the API's own test wallet, so they are not
 * repeated here.
 */

// The secret key 1, the same one backend/tests/siwe_wallet.py calls Wallet(1).
const ONE = new Uint8Array(32)
ONE[31] = 1
const ONE_ADDRESS = '0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf'

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

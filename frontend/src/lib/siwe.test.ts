import { describe, expect, it } from 'vitest'
import { siweMessage, toChecksumAddress, type SignInTerms } from './siwe'
import { addressOf, demoWallet, personalDigest, signPersonal } from './wallet'
import { secp256k1 } from '@noble/curves/secp256k1.js'
import { keccak_256 } from '@noble/hashes/sha3.js'
import { bytesToHex, hexToBytes } from '@noble/hashes/utils.js'

const terms: SignInTerms = {
  nonce: '0123456789abcdef01234567',
  domain: 'localhost:5173',
  uri: 'http://localhost:5173',
  chain_id: 5042002,
  statement: 'Sign in to Misthos. This costs nothing and moves no money.',
}

// The secret key 1, the same one backend/tests/siwe_wallet.py calls Wallet(1).
const ONE = new Uint8Array(32)
ONE[31] = 1
const ONE_ADDRESS = '0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf'

describe('toChecksumAddress', () => {
  it('matches the EIP-55 test vectors', () => {
    for (const address of [
      '0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed',
      '0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359',
      '0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB',
      '0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb',
    ]) {
      expect(toChecksumAddress(address.toLowerCase())).toBe(address)
    }
  })

  it('refuses something that is not an address', () => {
    expect(() => toChecksumAddress('0x1234')).toThrow()
  })
})

describe('siweMessage', () => {
  it('lays the message out the way the API parses it', () => {
    const message = siweMessage(terms, ONE_ADDRESS.toLowerCase(), new Date('2026-10-07T10:00:00Z'))
    expect(message.split('\n')).toEqual([
      'localhost:5173 wants you to sign in with your Ethereum account:',
      ONE_ADDRESS,
      '',
      'Sign in to Misthos. This costs nothing and moves no money.',
      '',
      'URI: http://localhost:5173',
      'Version: 1',
      'Chain ID: 5042002',
      'Nonce: 0123456789abcdef01234567',
      'Issued At: 2026-10-07T10:00:00.000Z',
      'Expiration Time: 2026-10-07T10:05:00.000Z',
    ])
  })
})

describe('signing', () => {
  it('derives the address a wallet would show', () => {
    expect(addressOf(ONE)).toBe(ONE_ADDRESS)
  })

  it('signs exactly as the API test wallet does, so the API recovers the address', () => {
    const message = siweMessage(terms, ONE_ADDRESS, new Date('2026-10-07T10:00:00Z'))
    // Wallet(1).sign(message) in backend/tests/siwe_wallet.py, which the API verifies.
    expect(signPersonal(ONE, message)).toBe(
      '0xa6ec9f4a447b01548a1240b83f642d1f724f7c620c07d4ea1ddc1d596711a7cc' +
        '34929f1eff162d9322b0aa4cf4f9d255361a151b9e0f2f53d833c507b62cbc191c',
    )
  })

  it('gives a demo wallet a key that signs for its own address', async () => {
    const wallet = demoWallet('contributor')
    const raw = hexToBytes((await wallet.sign('hello')).slice(2))
    const publicKey = secp256k1.Signature.fromBytes(
      Uint8Array.of(raw[64] - 27, ...raw.slice(0, 64)),
      'recovered',
    )
      .recoverPublicKey(personalDigest('hello'))
      .toBytes(false)
    const signer = '0x' + bytesToHex(keccak_256(publicKey.slice(1)).slice(-20))
    expect(toChecksumAddress(signer)).toBe(wallet.address)
  })
})

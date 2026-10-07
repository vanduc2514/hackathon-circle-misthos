import { secp256k1 } from '@noble/curves/secp256k1.js'
import { keccak_256 } from '@noble/hashes/sha3.js'
import { bytesToHex, concatBytes, hexToBytes, utf8ToBytes } from '@noble/hashes/utils.js'
import { browserProvider } from './eip1193'
import { toChecksumAddress } from './siwe'

/** Something that can prove it holds an address by signing a message. */
export type Signer = {
  kind: 'browser' | 'demo'
  address: string
  sign: (message: string) => Promise<string>
}

/** What `personal_sign` signs (EIP-191): a prefix, the length, the message. */
export function personalDigest(message: string): Uint8Array {
  const body = utf8ToBytes(message)
  const prefix = utf8ToBytes(`\x19Ethereum Signed Message:\n${body.length}`)
  return keccak_256(concatBytes(prefix, body))
}

export function addressOf(secretKey: Uint8Array): string {
  const publicKey = secp256k1.getPublicKey(secretKey, false)
  return toChecksumAddress('0x' + bytesToHex(keccak_256(publicKey.slice(1)).slice(-20)))
}

/** r ‖ s ‖ v with v = 27 + the recovery id, which is what a wallet returns. */
export function signPersonal(secretKey: Uint8Array, message: string): string {
  const recovered = secp256k1.sign(personalDigest(message), secretKey, {
    prehash: false,
    format: 'recovered',
  })
  // noble puts the recovery id first; Ethereum puts it last.
  const v = (27 + recovered[0]).toString(16)
  return '0x' + bytesToHex(recovered.slice(1)) + v
}

// ------------------------------------------------------------ browser wallet

export { browserProvider }

/** The wallet extension in this browser, such as MetaMask. */
export async function browserWallet(): Promise<Signer> {
  const provider = browserProvider()
  if (!provider) throw new Error('No wallet extension was found in this browser.')
  const accounts = (await provider.request({ method: 'eth_requestAccounts' })) as string[]
  if (!accounts?.length) throw new Error('The wallet did not share an account.')
  const address = accounts[0]
  return {
    kind: 'browser',
    address: toChecksumAddress(address),
    sign: async (message) =>
      (await provider.request({
        method: 'personal_sign',
        params: ['0x' + bytesToHex(utf8ToBytes(message)), address],
      })) as string,
  }
}

// --------------------------------------------------------------- demo wallets

export type DemoSlot = 'publisher' | 'contributor'

const slotKey = (slot: DemoSlot) => `misthos.demo-wallet.${slot}`

/**
 * A throwaway key kept in this browser, for the simulation only, so the demo can be
 * driven without a wallet extension. One per side, so a publisher and a contributor
 * can take turns in the same browser. It never holds funds.
 */
export function demoWallet(slot: DemoSlot): Signer {
  let hex: string | null = null
  try {
    hex = localStorage.getItem(slotKey(slot))
  } catch {
    // Storage can be blocked; a fresh key for this page is still a valid demo.
  }
  if (!hex || !/^[0-9a-f]{64}$/.test(hex)) {
    hex = bytesToHex(secp256k1.utils.randomSecretKey())
    try {
      localStorage.setItem(slotKey(slot), hex)
    } catch {
      // As above.
    }
  }
  const key = hexToBytes(hex)
  return { kind: 'demo', address: addressOf(key), sign: async (m) => signPersonal(key, m) }
}

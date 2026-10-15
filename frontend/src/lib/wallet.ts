import { secp256k1 } from '@noble/curves/secp256k1.js'
import { keccak_256 } from '@noble/hashes/sha3.js'
import { bytesToHex, concatBytes, hexToBytes, utf8ToBytes } from '@noble/hashes/utils.js'
import { browserProvider, type Eip1193 } from './eip1193'
import { injectedProvider } from './eip6963'
import { toChecksumAddress } from './siwe'

/** Something that can prove it holds an address by signing a message. */
export type Signer = {
  kind: 'browser' | 'metamask-app' | 'demo'
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

/** A signer over any EIP-1193 provider: ask for an account, then `personal_sign`. */
export async function signerFrom(provider: Eip1193, kind: Signer['kind']): Promise<Signer> {
  const accounts = (await provider.request({ method: 'eth_requestAccounts' })) as string[]
  if (!accounts?.length) throw new Error('The wallet did not share an account.')
  const address = accounts[0]
  return {
    kind,
    address: toChecksumAddress(address),
    sign: async (message) =>
      (await provider.request({
        method: 'personal_sign',
        params: ['0x' + bytesToHex(utf8ToBytes(message)), address],
      })) as string,
  }
}

/** The wallet extension in this browser, such as MetaMask. */
export async function browserWallet(): Promise<Signer> {
  const provider = browserProvider()
  if (!provider) throw new Error('No wallet extension was found in this browser.')
  return signerFrom(provider, 'browser')
}

/**
 * The MetaMask app, for a browser with no wallet in it (#141): on a phone it opens the
 * app and comes back, on a computer it shows a code to scan with the app. The SDK is
 * loaded only here, so a browser with an extension never downloads it.
 */
export async function metamaskApp(): Promise<Signer> {
  const { MetaMaskSDK } = await import('@metamask/sdk')
  const sdk = new MetaMaskSDK({
    dappMetadata: { name: 'Misthos', url: globalThis.location?.origin },
    checkInstallationImmediately: false,
  })
  await sdk.connect()
  const provider = sdk.getProvider()
  if (!provider) throw new Error('The MetaMask app did not connect.')
  return signerFrom(provider as unknown as Eip1193, 'metamask-app')
}

/**
 * The one "Sign in with your wallet" button (#141): a wallet in this page if one
 * announces itself or sits at `window.ethereum`, else the MetaMask app. The message
 * signed, the session and the API are the same whichever it is.
 */
export async function anyWallet(
  find: () => Promise<Eip1193 | null> = () => injectedProvider(),
  app: () => Promise<Signer> = metamaskApp,
): Promise<Signer> {
  const provider = await find()
  return provider ? signerFrom(provider, 'browser') : app()
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

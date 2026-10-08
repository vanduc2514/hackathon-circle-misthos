import { keccak_256 } from '@noble/hashes/sha3.js'
import { bytesToHex, utf8ToBytes } from '@noble/hashes/utils.js'

/** What the API asks a sign-in message to say, from `POST /auth/nonce`. */
export type SignInTerms = {
  nonce: string
  domain: string
  uri: string
  chain_id: number
  statement: string
}

/** EIP-55: the mixed-case form wallets display, checksummed with Keccak-256. */
export function toChecksumAddress(address: string): string {
  const lower = address.toLowerCase().replace(/^0x/, '')
  if (!/^[0-9a-f]{40}$/.test(lower)) throw new Error(`not an address: ${address}`)
  const digest = bytesToHex(keccak_256(utf8ToBytes(lower)))
  let out = '0x'
  for (let i = 0; i < lower.length; i++) {
    const c = lower[i]
    out += c >= 'a' && c <= 'f' && parseInt(digest[i], 16) >= 8 ? c.toUpperCase() : c
  }
  return out
}

/**
 * The Sign-In with Ethereum message (EIP-4361), line for line. The API parses this
 * exact layout in backend/src/misthos/auth/siwe.py, so change both or neither. It
 * names the domain, chain and nonce the API issued, which is what stops a signature
 * for another site, another chain or an earlier sign-in from working here.
 */
export function siweMessage(
  terms: SignInTerms,
  address: string,
  issuedAt: Date,
  validForMinutes = 5,
): string {
  const expires = new Date(issuedAt.getTime() + validForMinutes * 60_000)
  return [
    `${terms.domain} wants you to sign in with your Ethereum account:`,
    toChecksumAddress(address),
    '',
    terms.statement,
    '',
    `URI: ${terms.uri}`,
    'Version: 1',
    `Chain ID: ${terms.chain_id}`,
    `Nonce: ${terms.nonce}`,
    `Issued At: ${issuedAt.toISOString()}`,
    `Expiration Time: ${expires.toISOString()}`,
  ].join('\n')
}

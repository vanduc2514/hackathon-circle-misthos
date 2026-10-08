import { browserProvider } from './eip1193'

/** What the API asks to be sent for a plan (`PaymentRequest`). */
export type PaymentAsk = {
  amount_usdc: string
  pay_to: string
  payer: string
  chain_id: number
  usdc_address: string
}

/** A decimal USDC amount in base units, six decimals, without floating point. */
export function baseUnits(amount: string): bigint {
  const [whole, fraction = ''] = amount.trim().split('.')
  if (!/^\d+$/.test(whole) || !/^\d{0,6}$/.test(fraction)) {
    throw new Error(`not a USDC amount: ${amount}`)
  }
  return BigInt(whole) * 1_000_000n + BigInt(fraction.padEnd(6, '0'))
}

/** ERC-20 `transfer(to, amount)` call data. */
export function transferData(to: string, amount: bigint): string {
  const address = to.toLowerCase().replace(/^0x/, '')
  if (!/^[0-9a-f]{40}$/.test(address)) throw new Error(`not an address: ${to}`)
  return '0xa9059cbb' + address.padStart(64, '0') + amount.toString(16).padStart(64, '0')
}

/**
 * Send the payment from the wallet extension: switch it to Arc, then transfer USDC
 * through the token contract. Returns the transaction hash for the API to read.
 */
export async function payFromWallet(ask: PaymentAsk): Promise<string> {
  const provider = browserProvider()
  if (!provider) throw new Error('No wallet extension was found in this browser.')
  const accounts = (await provider.request({ method: 'eth_requestAccounts' })) as string[]
  if (!accounts.some((a) => a.toLowerCase() === ask.payer.toLowerCase())) {
    throw new Error(`Switch your wallet to ${ask.payer}, the wallet this plan is paid from.`)
  }
  try {
    await provider.request({
      method: 'wallet_switchEthereumChain',
      params: [{ chainId: '0x' + ask.chain_id.toString(16) }],
    })
  } catch {
    throw new Error(`Add Arc (chain ${ask.chain_id}) to your wallet, then try again.`)
  }
  return (await provider.request({
    method: 'eth_sendTransaction',
    params: [
      {
        from: ask.payer,
        to: ask.usdc_address,
        data: transferData(ask.pay_to, baseUnits(ask.amount_usdc)),
      },
    ],
  })) as string
}

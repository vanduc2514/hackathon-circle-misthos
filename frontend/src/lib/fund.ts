/**
 * Fund an issue from the publisher's own wallet.
 *
 * The platform never signs a commitment: it moves the publisher's USDC. Approving the
 * price records it as the escrow's ceiling, and the API then refuses to book the
 * funding until the commitment is on chain (`NotCommitted`). The wallet sends the two
 * calls the API spells out (the USDC allowance, then the commitment), and approving
 * again books the publisher's own transaction. The simulation needs none of this.
 */

import { ApiError } from './client'

export interface WalletCall {
  label: string
  to: string
  data: string
}

export interface CommitmentPlan {
  chain_id: number
  calls: WalletCall[]
}

/** The slice of an EIP-1193 provider this needs. */
export interface Wallet {
  request: (args: { method: string; params?: unknown[] }) => Promise<unknown>
}

/** True when the API refused only because the publisher has not committed yet. */
export function awaitsCommitment(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && /NotCommitted/.test(err.message)
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/** Send each call from the wallet, in order, waiting until each is mined. */
export async function sendFromWallet(
  wallet: Wallet,
  plan: CommitmentPlan,
  onStep: (label: string) => void = () => {},
  pollMs = 500,
): Promise<string[]> {
  const accounts = (await wallet.request({ method: 'eth_requestAccounts' })) as string[]
  const from = accounts?.[0]
  if (!from) throw new Error('The wallet did not share an account')
  await wallet.request({
    method: 'wallet_switchEthereumChain',
    params: [{ chainId: `0x${plan.chain_id.toString(16)}` }],
  })

  const hashes: string[] = []
  for (const call of plan.calls) {
    onStep(call.label)
    const hash = (await wallet.request({
      method: 'eth_sendTransaction',
      params: [{ from, to: call.to, data: call.data }],
    })) as string
    // The allowance must be mined before the commitment can spend it; on Arc that is
    // well under a second.
    for (;;) {
      const receipt = (await wallet.request({
        method: 'eth_getTransactionReceipt',
        params: [hash],
      })) as { status?: string } | null
      if (receipt) {
        if (receipt.status !== '0x1') throw new Error(`${call.label} failed on chain (${hash})`)
        break
      }
      await sleep(pollMs)
    }
    hashes.push(hash)
  }
  return hashes
}

/**
 * Approve the price; if the escrow is waiting for the publisher's commitment, have
 * the wallet send it, then approve again so the platform books it.
 */
export async function fundFromWallet<T>(
  fund: () => Promise<T>,
  plan: () => Promise<CommitmentPlan>,
  wallet: Wallet | null,
  onStep?: (label: string) => void,
): Promise<T> {
  try {
    return await fund()
  } catch (err) {
    if (!awaitsCommitment(err)) throw err
  }
  if (!wallet) throw new Error('Funding needs a browser wallet to send the commitment')
  await sendFromWallet(wallet, await plan(), onStep)
  return fund()
}

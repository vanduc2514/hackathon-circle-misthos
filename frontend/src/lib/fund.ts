/**
 * Fund an issue from the publisher's own wallet.
 *
 * The platform never signs a commitment: it moves the publisher's USDC. Approving the
 * price records it as the escrow's ceiling, and the API then refuses to book the
 * funding until the commitment is on chain (`NotCommitted`). The wallet sends the two
 * calls the API spells out (the USDC allowance, then the commitment), and approving
 * again books the publisher's own transaction.
 */

import { ApiError, type EscrowReadback } from './client'
import { arcChainInfo, switchChain } from './chain'

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

/**
 * True when the escrow holds a ceiling for the issue and no money against it, which is
 * what the publisher's own commitment is for.
 *
 * The escrow is asked rather than the refusal read. A refusal's sentence can be replaced
 * on the way to the browser -- a proxy that rewrites error bodies is enough -- and a
 * handoff that waits on that sentence never starts: the issue sits at its ceiling with
 * nothing committed and the publisher sees a status code and nothing else (#147).
 */
export function escrowAwaitsCommitment(escrow: EscrowReadback | null): boolean {
  return ceilingOf(escrow) > 0 && heldOf(escrow) === 0
}

function ceilingOf(escrow: EscrowReadback | null): number {
  return Number(escrow?.escrow_ceiling?.base_units ?? 0)
}

function heldOf(escrow: EscrowReadback | null): number {
  return Number(escrow?.amount?.base_units ?? 0)
}

/**
 * True when a refusal from the first approval is the one that waits on the publisher.
 *
 * The status carries it: `/fund` answers 409 once the ceiling is recorded, and the status
 * survives a body that did not. The sentence is a bonus and never the test, so whatever
 * replaced it cannot quietly turn the handoff off (#147).
 */
export function refusedForCommitment(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * Move the wallet onto Arc, offering to add it when the wallet has never been there.
 *
 * A publisher may hold their USDC on a CCTP source chain and never have added Arc, and a
 * wallet refuses to switch to a chain it does not know (4902), so the commitment could
 * not be sent at all. Arc's own metadata comes from viem, not from strings here.
 */
async function switchToArc(wallet: Wallet, chainId: number): Promise<void> {
  const chain = arcChainInfo(chainId)
  if (chain) {
    await switchChain(wallet, chain)
    return
  }
  await wallet.request({
    method: 'wallet_switchEthereumChain',
    params: [{ chainId: `0x${chainId.toString(16)}` }],
  })
}

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
  await switchToArc(wallet, plan.chain_id)

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

/** The three calls funding makes into the API, so the decision below can be read from it. */
export interface FundingCalls<T> {
  /** Approve the price. The API answers 409 while the commitment is still owed. */
  fund: () => Promise<T>
  /** The two transactions the publisher's own wallet sends, in order. */
  plan: () => Promise<CommitmentPlan>
  /** The escrow as the API reads it back, to see whether the commitment is still owed. */
  escrow: () => Promise<EscrowReadback>
}

/**
 * True when the commitment is still owed. The refusal already said so, and the escrow is
 * the authority; a read that fails is no reason to withhold what the publisher asked for.
 */
async function commitmentUnsent<T>(calls: FundingCalls<T>): Promise<boolean> {
  try {
    return escrowAwaitsCommitment(await calls.escrow())
  } catch {
    return true
  }
}

/**
 * Approve the price; if the escrow is waiting for the publisher's commitment, have
 * the wallet send it, then approve again so the platform books it.
 */
export async function fundFromWallet<T>(
  calls: FundingCalls<T>,
  wallet: Wallet | null,
  onStep?: (label: string) => void,
): Promise<T> {
  try {
    return await calls.fund()
  } catch (err) {
    if (!refusedForCommitment(err)) throw err
    // Ask the escrow before putting two prompts in front of the publisher: a 409 from
    // somewhere else, an issue already funded or another action in flight, would send
    // work the contract can only revert. What was refused is then passed on untouched.
    if (!(await commitmentUnsent(calls))) throw err
  }
  if (!wallet) throw new Error('Funding needs a browser wallet to send the commitment')
  await sendFromWallet(wallet, await calls.plan(), onStep)
  return calls.fund()
}

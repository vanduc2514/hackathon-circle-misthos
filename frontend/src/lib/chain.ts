/**
 * Moving the wallet onto the chain a step needs, adding the chain when the wallet has
 * never seen it.
 *
 * Bridging and funding both need this, and both start from a wallet that has never been
 * to Arc: a publisher who holds USDC on a CCTP source chain has no Arc entry, so a plain
 * `wallet_switchEthereumChain` is refused with 4902 and the step cannot be taken. What a
 * chain is called, and where it can be reached, comes from viem's own definitions rather
 * than from strings typed here.
 */

import { arc, arcTestnet } from 'viem/chains'

/**
 * The one thing a chain switch needs of a wallet.
 *
 * The argument is `never` so that any provider fits: viem types its provider per JSON-RPC
 * method and the loose slice the rest of this app passes around is not, so neither is
 * assignable to the other. The calls below are the only ones this makes.
 */
export interface ChainSwitcher {
  request: (args: never) => Promise<unknown>
}

/** What a wallet needs to add a chain it does not know (EIP-3085). */
export interface ChainInfo {
  chainId: number
  name: string
  nativeCurrency: { name: string; symbol: string; decimals: number }
  rpcEndpoints: readonly string[]
  explorerUrl?: string
}

/** Arc, as viem defines it. ADR-8: Arc is the only settlement chain. */
export const ARC_CHAINS: Record<number, ChainInfo> = {
  [arcTestnet.id]: {
    chainId: arcTestnet.id,
    name: arcTestnet.name,
    nativeCurrency: arcTestnet.nativeCurrency,
    rpcEndpoints: arcTestnet.rpcUrls.default.http,
    explorerUrl: arcTestnet.blockExplorers?.default?.url,
  },
  [arc.id]: {
    chainId: arc.id,
    name: arc.name,
    nativeCurrency: arc.nativeCurrency,
    rpcEndpoints: arc.rpcUrls.default.http,
    explorerUrl: arc.blockExplorers?.default?.url,
  },
}

/** The metadata for one of Arc's chains, or null when the id is not one of them. */
export function arcChainInfo(chainId: number): ChainInfo | null {
  return ARC_CHAINS[chainId] ?? null
}

const UNKNOWN_CHAIN = 4902

function hex(n: number): string {
  return `0x${n.toString(16)}`
}

/**
 * Switch the wallet to `chain`, adding it first if the wallet has never seen it
 * (EIP-3326 error 4902): Amoy and Fuji are not preconfigured everywhere, and neither is
 * Arc. Adding a chain is the wallet's decision, so this can still be refused.
 */
export async function switchChain(provider: ChainSwitcher, chain: ChainInfo): Promise<void> {
  const chainId = hex(chain.chainId)
  try {
    await provider.request({
      method: 'wallet_switchEthereumChain',
      params: [{ chainId }],
    } as never)
  } catch (err) {
    if ((err as { code?: unknown })?.code !== UNKNOWN_CHAIN) throw err
    await provider.request({
      method: 'wallet_addEthereumChain',
      params: [
        {
          chainId,
          chainName: chain.name,
          nativeCurrency: chain.nativeCurrency,
          rpcUrls: [...chain.rpcEndpoints],
          blockExplorerUrls: chain.explorerUrl
            ? [chain.explorerUrl.replace(/\/tx\/\{hash\}$/, '')]
            : undefined,
        },
      ],
    } as never)
  }
}

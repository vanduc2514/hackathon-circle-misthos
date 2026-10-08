/**
 * Bridge a publisher's USDC to Arc over CCTP, from their own browser wallet.
 *
 * The publisher signs the source-chain steps (approve, burn) with their own wallet,
 * and the USDC is minted to the same address on Arc. The platform never holds it on
 * either chain, which is why this lives in the browser and not in the API.
 *
 * Arc's gas token is USDC, and the publisher this exists for holds none there yet,
 * so the mint on Arc goes through Circle's Forwarding Service: Circle fetches the
 * attestation and submits the mint, and its fee comes out of the bridged amount.
 * Without it the burn would succeed and the mint would have nothing to pay gas with.
 *
 * Nothing here runs except from an explicit click.
 */

import type { EIP1193Provider } from 'viem'

export const ARC_DESTINATION = 'Arc_Testnet'

/** CCTP testnet sources a publisher is likely to hold USDC on. */
export const SOURCE_CHAINS = [
  { key: 'Ethereum_Sepolia', label: 'Ethereum Sepolia' },
  { key: 'Base_Sepolia', label: 'Base Sepolia' },
  { key: 'Arbitrum_Sepolia', label: 'Arbitrum Sepolia' },
  { key: 'Optimism_Sepolia', label: 'Optimism Sepolia' },
  { key: 'Avalanche_Fuji', label: 'Avalanche Fuji' },
  { key: 'Polygon_Amoy_Testnet', label: 'Polygon Amoy' },
] as const

export type SourceChain = (typeof SOURCE_CHAINS)[number]['key']

/** Above this, the panel asks the publisher to look twice before signing. */
export const LARGE_TRANSFER_USDC = 100

export type BridgeCheck =
  | { ok: true; amount: string; large: boolean }
  | { ok: false; reason: string }

const USDC_AMOUNT = /^\d+(\.\d{1,6})?$/

/** Validate a bridge request before any wallet is touched. */
export function checkBridge(source: string, amount: string): BridgeCheck {
  if (!SOURCE_CHAINS.some((c) => c.key === source)) {
    return { ok: false, reason: `${source} is not a supported source chain` }
  }
  const trimmed = amount.trim()
  if (!USDC_AMOUNT.test(trimmed)) {
    return { ok: false, reason: 'Enter an amount in USDC, with at most 6 decimals' }
  }
  if (Number(trimmed) <= 0) {
    return { ok: false, reason: 'The amount must be more than zero' }
  }
  return { ok: true, amount: trimmed, large: Number(trimmed) > LARGE_TRANSFER_USDC }
}

export interface BridgeStep {
  name: string
  state: string
  txHash?: string
  explorerUrl?: string
  error?: unknown
}

/**
 * The SDK's result, kept whole. `retry` needs all of it (provider, source,
 * destination, token), not just the steps a panel shows.
 */
export interface BridgeOutcome {
  state: string
  steps: BridgeStep[]
  [field: string]: unknown
}

/** What a wallet needs to add a chain it does not know (EIP-3085). */
export interface ChainInfo {
  chainId: number
  name: string
  nativeCurrency: { name: string; symbol: string; decimals: number }
  rpcEndpoints: readonly string[]
  explorerUrl?: string
}

/** The calls this module makes into Circle's SDK, so tests can stand in. */
export interface BridgeDeps {
  chainInfo: (source: SourceChain) => Promise<ChainInfo>
  adapterFor: (provider: EIP1193Provider) => Promise<unknown>
  bridge: (params: {
    from: { adapter: unknown; chain: string }
    to: { recipientAddress: string; chain: string; useForwarder: true }
    amount: string
  }) => Promise<BridgeOutcome>
  retry: (result: BridgeOutcome, adapter: unknown) => Promise<BridgeOutcome>
}

type ChainModule = Record<string, unknown>

/** Bridge Kit's chain definitions name chains by `chain`; find one by that key. */
export function findChain(chains: ChainModule, key: string): ChainInfo & { chain: string } {
  const found = Object.values(chains).find(
    (c): c is ChainInfo & { chain: string } =>
      typeof c === 'object' && c !== null && (c as { chain?: unknown }).chain === key,
  )
  if (!found) throw new Error(`Bridge Kit has no chain ${key}`)
  return found
}

export async function circleDeps(): Promise<BridgeDeps> {
  // Loaded on click, so the SDK is not in the bundle every page pays for.
  const [{ BridgeKit }, { createViemAdapterFromProvider }, chains] = await Promise.all([
    import('@circle-fin/bridge-kit'),
    import('@circle-fin/adapter-viem-v2'),
    import('@circle-fin/bridge-kit/chains'),
  ])
  const kit = new BridgeKit()
  type Params = Parameters<typeof kit.bridge>[0]
  type Retried = Parameters<typeof kit.retry>
  return {
    chainInfo: async (source) => findChain(chains as ChainModule, source),
    adapterFor: (provider) => createViemAdapterFromProvider({ provider }),
    bridge: async (params) => (await kit.bridge(params as Params)) as unknown as BridgeOutcome,
    retry: async (result, adapter) =>
      (await kit.retry(result as unknown as Retried[0], {
        from: adapter,
        to: adapter,
      } as Retried[1])) as unknown as BridgeOutcome,
  }
}

const UNKNOWN_CHAIN = 4902

function hex(n: number): string {
  return `0x${n.toString(16)}`
}

/**
 * Switch the wallet to the source chain, adding it first if the wallet has never
 * seen it (EIP-3326 error 4902): Amoy and Fuji are not preconfigured everywhere.
 */
export async function switchTo(provider: EIP1193Provider, chain: ChainInfo): Promise<void> {
  const chainId = hex(chain.chainId)
  try {
    await provider.request({ method: 'wallet_switchEthereumChain', params: [{ chainId }] })
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

/**
 * Move `amount` USDC from `source` to the same address on Arc, minted by Circle's
 * forwarder. A soft failure comes back as a result with its steps, never thrown
 * away: re-running from scratch could burn twice. Resume it with `resumeBridge`.
 */
export async function bridgeToArc(
  provider: EIP1193Provider,
  source: SourceChain,
  amount: string,
  deps?: BridgeDeps,
): Promise<BridgeOutcome> {
  const check = checkBridge(source, amount)
  if (!check.ok) throw new Error(check.reason)

  const sdk = deps ?? (await circleDeps())
  const accounts = (await provider.request({ method: 'eth_requestAccounts' })) as string[]
  const recipient = accounts?.[0]
  if (!recipient) throw new Error('The wallet did not share an account')

  await switchTo(provider, await sdk.chainInfo(source))
  const adapter = await sdk.adapterFor(provider)
  return sdk.bridge({
    from: { adapter, chain: source },
    to: { recipientAddress: recipient, chain: ARC_DESTINATION, useForwarder: true },
    amount: check.amount,
  })
}

/** Resume a bridge that stopped part way, from the step that failed. */
export async function resumeBridge(
  provider: EIP1193Provider,
  result: BridgeOutcome,
  deps?: BridgeDeps,
): Promise<BridgeOutcome> {
  const sdk = deps ?? (await circleDeps())
  return sdk.retry(result, await sdk.adapterFor(provider))
}

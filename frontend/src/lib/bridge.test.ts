import { describe, expect, it, vi } from 'vitest'
import type { EIP1193Provider } from 'viem'
import * as chains from '@circle-fin/bridge-kit/chains'
import {
  ARC_DESTINATION,
  SOURCE_CHAINS,
  bridgeToArc,
  checkBridge,
  findChain,
  resumeBridge,
  switchTo,
  type BridgeDeps,
  type ChainInfo,
} from './bridge'

const ACCOUNT = '0x00000000000000000000000000000000000b0b00'

const BASE_SEPOLIA: ChainInfo = {
  chainId: 84532,
  name: 'Base Sepolia',
  nativeCurrency: { name: 'Sepolia Ether', symbol: 'ETH', decimals: 18 },
  rpcEndpoints: ['https://sepolia.base.org'],
  explorerUrl: 'https://sepolia.basescan.org/tx/{hash}',
}

function fakeWallet(onSwitch?: () => void) {
  const calls: { method: string; params?: unknown }[] = []
  const provider = {
    request: async (args: { method: string; params?: unknown }) => {
      calls.push(args)
      if (args.method === 'eth_requestAccounts') return [ACCOUNT]
      if (args.method === 'wallet_switchEthereumChain') onSwitch?.()
      return null
    },
  } as unknown as EIP1193Provider
  return { provider, calls }
}

function fakeKit(outcome = { state: 'success', steps: [{ name: 'mint', state: 'success' }] }) {
  const bridged: Parameters<BridgeDeps['bridge']>[0][] = []
  const retried: unknown[][] = []
  const deps: BridgeDeps = {
    chainInfo: async () => BASE_SEPOLIA,
    adapterFor: async () => 'adapter',
    bridge: async (params) => {
      bridged.push(params)
      return outcome
    },
    retry: async (result, adapter) => {
      retried.push([result, adapter])
      return { state: 'success', steps: [] }
    },
  }
  return { deps, bridged, retried }
}

describe('checkBridge', () => {
  it('accepts a supported source and a USDC amount', () => {
    expect(checkBridge('Base_Sepolia', '25.50')).toEqual({ ok: true, amount: '25.50', large: false })
  })

  it('flags transfers over 100 USDC so the publisher looks twice', () => {
    expect(checkBridge('Base_Sepolia', '250')).toMatchObject({ ok: true, large: true })
  })

  it('refuses more precision than USDC has', () => {
    expect(checkBridge('Base_Sepolia', '1.0000001')).toMatchObject({ ok: false })
  })

  it('refuses zero, negatives and text', () => {
    for (const amount of ['0', '-1', 'ten', '']) {
      expect(checkBridge('Base_Sepolia', amount)).toMatchObject({ ok: false })
    }
  })

  it('refuses a chain it does not bridge from, including Arc itself', () => {
    expect(checkBridge(ARC_DESTINATION, '10')).toMatchObject({ ok: false })
    expect(checkBridge('Base', '10')).toMatchObject({ ok: false })
  })
})

describe("Bridge Kit's chain definitions", () => {
  it('know every source chain offered, so a wallet can be switched to or given it', () => {
    for (const { key } of SOURCE_CHAINS) {
      const chain = findChain(chains, key)
      expect(chain.chainId).toBeGreaterThan(0)
      expect(chain.rpcEndpoints.length).toBeGreaterThan(0)
    }
  })

  it('let Arc testnet be a forwarder destination, which is what pays the mint', () => {
    const arc = findChain(chains, ARC_DESTINATION) as unknown as {
      chainId: number
      cctp: { forwarderSupported: { destination: boolean } }
    }
    expect(arc.chainId).toBe(5042002)
    expect(arc.cctp.forwarderSupported.destination).toBe(true)
  })
})

describe('switchTo', () => {
  it('adds a chain the wallet does not know, then carries on', async () => {
    const calls: { method: string; params?: unknown }[] = []
    const provider = {
      request: async (args: { method: string; params?: unknown }) => {
        calls.push(args)
        if (args.method === 'wallet_switchEthereumChain') throw { code: 4902 }
        return null
      },
    } as unknown as EIP1193Provider

    await switchTo(provider, BASE_SEPOLIA)

    expect(calls.map((c) => c.method)).toEqual(['wallet_switchEthereumChain', 'wallet_addEthereumChain'])
    expect(calls[1].params).toEqual([
      {
        chainId: '0x14a34',
        chainName: 'Base Sepolia',
        nativeCurrency: BASE_SEPOLIA.nativeCurrency,
        rpcUrls: ['https://sepolia.base.org'],
        blockExplorerUrls: ['https://sepolia.basescan.org'],
      },
    ])
  })

  it('passes on any other refusal, such as the user rejecting the switch', async () => {
    const provider = {
      request: async () => {
        throw { code: 4001 }
      },
    } as unknown as EIP1193Provider
    await expect(switchTo(provider, BASE_SEPOLIA)).rejects.toEqual({ code: 4001 })
  })
})

describe('bridgeToArc', () => {
  it('burns on the source chain and has the forwarder mint to the same address on Arc', async () => {
    const { provider, calls } = fakeWallet()
    const { deps, bridged } = fakeKit()

    const result = await bridgeToArc(provider, 'Base_Sepolia', '25', deps)

    expect(calls.map((c) => c.method)).toEqual(['eth_requestAccounts', 'wallet_switchEthereumChain'])
    expect(calls[1].params).toEqual([{ chainId: '0x14a34' }])
    expect(bridged).toEqual([
      {
        from: { adapter: 'adapter', chain: 'Base_Sepolia' },
        to: { recipientAddress: ACCOUNT, chain: 'Arc_Testnet', useForwarder: true },
        amount: '25',
      },
    ])
    expect(result.state).toBe('success')
  })

  it('never touches the wallet for an invalid request', async () => {
    const { provider, calls } = fakeWallet()
    const { deps, bridged } = fakeKit()

    await expect(bridgeToArc(provider, 'Base_Sepolia', '0', deps)).rejects.toThrow()
    expect(calls).toEqual([])
    expect(bridged).toEqual([])
  })

  it('keeps a soft failure whole, so it can be resumed rather than run again', async () => {
    const { provider } = fakeWallet()
    const partial = {
      state: 'error',
      steps: [
        { name: 'approve', state: 'success' },
        { name: 'burn', state: 'success', txHash: '0xBURN' },
        { name: 'fetchAttestation', state: 'error' },
      ],
      provider: 'CCTPV2BridgingProvider',
      source: { chain: 'Base_Sepolia' },
      destination: { chain: 'Arc_Testnet' },
    }
    const { deps, retried } = fakeKit(partial)

    const result = await bridgeToArc(provider, 'Base_Sepolia', '5', deps)
    expect(result).toEqual(partial)

    await resumeBridge(provider, result, deps)
    expect(retried).toEqual([[partial, 'adapter']])
  })
})

const sdk = vi.hoisted(() => ({
  bridge: vi.fn(async () => ({ state: 'success', steps: [] })),
  retry: vi.fn(async () => ({ state: 'success', steps: [] })),
  adapter: vi.fn(async () => 'viem-adapter'),
}))

vi.mock('@circle-fin/bridge-kit', () => ({
  BridgeKit: class {
    bridge = sdk.bridge
    retry = sdk.retry
  },
}))

vi.mock('@circle-fin/adapter-viem-v2', () => ({
  createViemAdapterFromProvider: sdk.adapter,
}))

describe('the wiring into Circle', () => {
  it('builds the adapter from the wallet and hands Bridge Kit the forwarder destination', async () => {
    const { provider } = fakeWallet()
    await bridgeToArc(provider, 'Avalanche_Fuji', '10')

    expect(sdk.adapter).toHaveBeenCalledWith({ provider })
    expect(sdk.bridge).toHaveBeenCalledWith({
      from: { adapter: 'viem-adapter', chain: 'Avalanche_Fuji' },
      to: { recipientAddress: ACCOUNT, chain: 'Arc_Testnet', useForwarder: true },
      amount: '10',
    })
  })

  it('resumes through kit.retry with the whole result and the wallet adapter', async () => {
    const { provider } = fakeWallet()
    const result = { state: 'error', steps: [], provider: 'x' }
    await resumeBridge(provider, result)
    expect(sdk.retry).toHaveBeenCalledWith(result, { from: 'viem-adapter', to: 'viem-adapter' })
  })
})

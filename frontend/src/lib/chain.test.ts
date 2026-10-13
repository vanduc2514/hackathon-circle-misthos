import { describe, expect, it } from 'vitest'
import { arcChainInfo, switchChain, type ChainInfo } from './chain'

const BASE_SEPOLIA: ChainInfo = {
  chainId: 84532,
  name: 'Base Sepolia',
  nativeCurrency: { name: 'Sepolia Ether', symbol: 'ETH', decimals: 18 },
  rpcEndpoints: ['https://sepolia.base.org'],
  explorerUrl: 'https://sepolia.basescan.org/tx/{hash}',
}

describe('arcChainInfo', () => {
  it('describes Arc testnet the way a wallet needs it to add the chain', () => {
    const chain = arcChainInfo(5042002)
    expect(chain?.name).toBe('Arc Testnet')
    expect(chain?.nativeCurrency.symbol).toBe('USDC')
    expect(chain?.rpcEndpoints.length).toBeGreaterThan(0)
    expect(chain?.explorerUrl).toBeTruthy()
  })

  it('describes Arc mainnet too, so neither is a string typed here', () => {
    expect(arcChainInfo(5042)?.name).toBe('Arc')
  })

  it('knows nothing of a chain that is not one of the Arc chains', () => {
    expect(arcChainInfo(84532)).toBeNull()
    expect(arcChainInfo(1)).toBeNull()
  })
})

describe('switchChain', () => {
  it('switches, and adds nothing, when the wallet already knows the chain', async () => {
    const calls: { method: string; params?: unknown }[] = []
    const provider = {
      request: async (args: { method: string; params?: unknown }) => {
        calls.push(args)
        return null
      },
    }

    await switchChain(provider, BASE_SEPOLIA)

    expect(calls).toEqual([
      { method: 'wallet_switchEthereumChain', params: [{ chainId: '0x14a34' }] },
    ])
  })

  it('adds a chain the wallet does not know, then carries on', async () => {
    const calls: { method: string; params?: unknown }[] = []
    const provider = {
      request: async (args: { method: string; params?: unknown }) => {
        calls.push(args)
        if (args.method === 'wallet_switchEthereumChain') throw { code: 4902 }
        return null
      },
    }

    await switchChain(provider, BASE_SEPOLIA)

    expect(calls.map((c) => c.method)).toEqual([
      'wallet_switchEthereumChain',
      'wallet_addEthereumChain',
    ])
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
    }
    await expect(switchChain(provider, BASE_SEPOLIA)).rejects.toEqual({ code: 4001 })
  })

  it('adds Arc testnet when the wallet has never been there, which funding needs', async () => {
    const calls: { method: string; params?: unknown }[] = []
    const provider = {
      request: async (args: { method: string; params?: unknown }) => {
        calls.push(args)
        if (args.method === 'wallet_switchEthereumChain') throw { code: 4902 }
        return null
      },
    }

    await switchChain(provider, arcChainInfo(5042002) as ChainInfo)

    expect(calls.map((c) => c.method)).toEqual([
      'wallet_switchEthereumChain',
      'wallet_addEthereumChain',
    ])
    expect(calls[1].params).toMatchObject([
      { chainId: '0x4cef52', chainName: 'Arc Testnet' },
    ])
  })
})

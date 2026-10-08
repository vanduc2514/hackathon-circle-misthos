import { describe, expect, it } from 'vitest'
import { ApiError } from './client'
import { awaitsCommitment, fundFromWallet, sendFromWallet, type Wallet } from './fund'

const PLAN = {
  chain_id: 5042002,
  calls: [
    { label: 'Let the escrow take 55.00 USDC', to: '0xUSDC', data: '0xapprove' },
    { label: 'Commit 55.00 USDC to ISS-1', to: '0xESCROW', data: '0xcommit' },
  ],
}

function fakeWallet(status = '0x1') {
  const calls: { method: string; params?: unknown[] }[] = []
  let sent = 0
  const wallet: Wallet = {
    request: async (args) => {
      calls.push(args)
      switch (args.method) {
        case 'eth_requestAccounts':
          return ['0xPUBLISHER']
        case 'eth_sendTransaction':
          sent += 1
          return `0xtx${sent}`
        case 'eth_getTransactionReceipt':
          return { status }
        default:
          return null
      }
    },
  }
  return { wallet, calls }
}

describe('awaitsCommitment', () => {
  it('recognises only the refusal that waits on the publisher', () => {
    expect(awaitsCommitment(new ApiError(409, 'the escrow refused: NotCommitted: …'))).toBe(true)
    expect(awaitsCommitment(new ApiError(409, 'the escrow refused: ExceedsCeiling(1, 0)'))).toBe(false)
    expect(awaitsCommitment(new ApiError(500, 'NotCommitted'))).toBe(false)
    expect(awaitsCommitment(new Error('NotCommitted'))).toBe(false)
  })
})

describe('sendFromWallet', () => {
  it('switches to the chain and sends the allowance before the commitment, from the wallet', async () => {
    const { wallet, calls } = fakeWallet()
    const hashes = await sendFromWallet(wallet, PLAN, () => {}, 0)

    expect(hashes).toEqual(['0xtx1', '0xtx2'])
    expect(calls[1]).toEqual({
      method: 'wallet_switchEthereumChain',
      params: [{ chainId: '0x4cef52' }],
    })
    const sent = calls.filter((c) => c.method === 'eth_sendTransaction').map((c) => c.params?.[0])
    expect(sent).toEqual([
      { from: '0xPUBLISHER', to: '0xUSDC', data: '0xapprove' },
      { from: '0xPUBLISHER', to: '0xESCROW', data: '0xcommit' },
    ])
  })

  it('stops at a call that fails on chain rather than sending the next', async () => {
    const { wallet, calls } = fakeWallet('0x0')
    await expect(sendFromWallet(wallet, PLAN, () => {}, 0)).rejects.toThrow(/failed on chain/)
    expect(calls.filter((c) => c.method === 'eth_sendTransaction')).toHaveLength(1)
  })
})

describe('fundFromWallet', () => {
  it('books straight away when nothing waits on the wallet, as in the simulation', async () => {
    const { wallet, calls } = fakeWallet()
    const result = await fundFromWallet(async () => 'funded', async () => PLAN, wallet)
    expect(result).toBe('funded')
    expect(calls).toEqual([])
  })

  it('commits from the wallet when the escrow waits for it, then approves again', async () => {
    const { wallet, calls } = fakeWallet()
    let attempts = 0
    const fund = async () => {
      attempts += 1
      if (attempts === 1) throw new ApiError(409, 'the escrow refused: NotCommitted: not yet')
      return 'funded'
    }
    const result = await fundFromWallet(fund, async () => PLAN, wallet)
    expect(result).toBe('funded')
    expect(attempts).toBe(2)
    expect(calls.filter((c) => c.method === 'eth_sendTransaction')).toHaveLength(2)
  })

  it('passes any other refusal on untouched', async () => {
    const refusal = new ApiError(409, 'the escrow refused: ExceedsCeiling(1, 0)')
    await expect(
      fundFromWallet(async () => Promise.reject(refusal), async () => PLAN, fakeWallet().wallet),
    ).rejects.toBe(refusal)
  })

  it('says a wallet is needed rather than failing obscurely', async () => {
    const fund = async () => Promise.reject(new ApiError(409, 'NotCommitted'))
    await expect(fundFromWallet(fund, async () => PLAN, null)).rejects.toThrow(/browser wallet/)
  })
})

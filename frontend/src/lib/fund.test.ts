import { describe, expect, it } from 'vitest'
import { ApiError, type EscrowReadback } from './client'
import {
  escrowAwaitsCommitment,
  fundFromWallet,
  refusedForCommitment,
  sendFromWallet,
  type FundingCalls,
  type Wallet,
} from './fund'

const PLAN = {
  chain_id: 5042002,
  calls: [
    { label: 'Let the escrow take 55.00 USDC', to: '0xUSDC', data: '0xapprove' },
    { label: 'Commit 55.00 USDC to ISS-1', to: '0xESCROW', data: '0xcommit' },
  ],
}

/** An issue whose price was approved: the escrow holds the ceiling and nothing else. */
const CEILING_ONLY = {
  status: 'none',
  amount: { usdc: '0.00', base_units: 0 },
  escrow_ceiling: { usdc: '55.00', base_units: 55_000_000 },
} as unknown as EscrowReadback

/** An issue whose commitment is already in the escrow. */
const HELD = {
  status: 'held',
  amount: { usdc: '55.00', base_units: 55_000_000 },
  escrow_ceiling: { usdc: '55.00', base_units: 55_000_000 },
} as unknown as EscrowReadback

const funding = (fund: () => Promise<string>, escrow = async () => CEILING_ONLY): FundingCalls<string> => ({
  fund,
  plan: async () => PLAN,
  escrow,
})

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

describe('escrowAwaitsCommitment', () => {
  it('is true when the ceiling is recorded and no money is held', () => {
    expect(escrowAwaitsCommitment(CEILING_ONLY)).toBe(true)
  })

  it('is false once the commitment is in the escrow', () => {
    expect(escrowAwaitsCommitment(HELD)).toBe(false)
  })

  it('is false when nothing was approved, so there is no ceiling to commit', () => {
    const none = { status: 'none', amount: {}, escrow_ceiling: null } as unknown as EscrowReadback
    expect(escrowAwaitsCommitment(none)).toBe(false)
    expect(escrowAwaitsCommitment(null)).toBe(false)
  })
})

describe('refusedForCommitment', () => {
  it('reads the status, which survives where a body may not', () => {
    expect(refusedForCommitment(new ApiError(409, '409'))).toBe(true)
    expect(refusedForCommitment(new ApiError(409, 'the escrow refused: NotCommitted: …'))).toBe(true)
  })

  it('leaves a refusal that is not a conflict alone', () => {
    expect(refusedForCommitment(new ApiError(500, 'NotCommitted'))).toBe(false)
    expect(refusedForCommitment(new ApiError(403, 'only PUB-100 can fund this issue'))).toBe(false)
    expect(refusedForCommitment(new Error('NotCommitted'))).toBe(false)
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

  it('has the wallet add Arc when it has never been there, so the commitment can be sent', async () => {
    const calls: { method: string; params?: unknown[] }[] = []
    let sent = 0
    const wallet: Wallet = {
      request: async (args) => {
        calls.push(args)
        if (args.method === 'wallet_switchEthereumChain') throw { code: 4902 }
        if (args.method === 'eth_requestAccounts') return ['0xPUBLISHER']
        if (args.method === 'eth_sendTransaction') return `0xtx${(sent += 1)}`
        if (args.method === 'eth_getTransactionReceipt') return { status: '0x1' }
        return null
      },
    }

    await sendFromWallet(wallet, PLAN, () => {}, 0)

    expect(calls[1]).toEqual({
      method: 'wallet_switchEthereumChain',
      params: [{ chainId: '0x4cef52' }],
    })
    expect(calls[2]).toMatchObject({
      method: 'wallet_addEthereumChain',
      params: [{ chainId: '0x4cef52', chainName: 'Arc Testnet' }],
    })
    expect(calls.filter((c) => c.method === 'eth_sendTransaction')).toHaveLength(2)
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
    const result = await fundFromWallet(funding(async () => 'funded'), wallet)
    expect(result).toBe('funded')
    expect(calls).toEqual([])
  })

  it('commits from the wallet when the escrow waits for it, then approves again', async () => {
    const { wallet, calls: walletCalls } = fakeWallet()
    let attempts = 0
    const fund = async () => {
      attempts += 1
      if (attempts === 1) throw new ApiError(409, 'the escrow refused: NotCommitted: not yet')
      return 'funded'
    }
    const result = await fundFromWallet(funding(fund), wallet)
    expect(result).toBe('funded')
    expect(attempts).toBe(2)
    expect(walletCalls.filter((c) => c.method === 'eth_sendTransaction')).toHaveLength(2)
  })

  it('still commits when the refusal arrived with its reason replaced (#147)', async () => {
    // What the deployment does: the proxy rewrites the body, so `unwrap` can only see the
    // status and the message the app falls back to is the bare code. Before #147 was
    // fixed the handoff matched `NotCommitted` in that message, found nothing, and gave
    // up: the ceiling stayed recorded, no commitment was ever sent, and the publisher was
    // left looking at a status code.
    const { wallet, calls: walletCalls } = fakeWallet()
    let attempts = 0
    const fund = async () => {
      attempts += 1
      if (attempts === 1) throw new ApiError(409, '409', null, 'Bad Gateway', '80dc7d66')
      return 'funded'
    }
    const result = await fundFromWallet(funding(fund), wallet)

    expect(result).toBe('funded')
    expect(attempts).toBe(2)
    expect(walletCalls.filter((c) => c.method === 'eth_sendTransaction')).toHaveLength(2)
  })

  it('passes a refusal on untouched when the escrow already holds the money', async () => {
    const refusal = new ApiError(409, 'another action on ISS-1 is in progress; retry in a moment')
    await expect(
      fundFromWallet(funding(async () => Promise.reject(refusal), async () => HELD), fakeWallet().wallet),
    ).rejects.toBe(refusal)
  })

  it('asks the escrow before the wallet, so no prompt appears for work that cannot be sent', async () => {
    const { wallet, calls: walletCalls } = fakeWallet()
    const refusal = new ApiError(409, '409')
    await expect(
      fundFromWallet(funding(async () => Promise.reject(refusal), async () => HELD), wallet),
    ).rejects.toBe(refusal)
    expect(walletCalls).toEqual([])
  })

  it('says a wallet is needed rather than failing obscurely', async () => {
    const fund = async () => Promise.reject(new ApiError(409, 'NotCommitted'))
    await expect(fundFromWallet(funding(fund), null)).rejects.toThrow(/browser wallet/)
  })
})

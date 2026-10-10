import { describe, expect, it } from 'vitest'
import { describeValueMoved, valueMovedStat, type ValueMoved } from './value-moved'

function row(over: Partial<ValueMoved>): ValueMoved {
  return {
    chain: 'arc-testnet',
    money: 'test',
    settled_issues: 1,
    settled_usdc: '1250.00',
    platform_fees_usdc: '125.00',
    ...over,
  }
}

describe('describeValueMoved', () => {
  it('names what the money was and where, so test money never reads as real', () => {
    expect(describeValueMoved(row({}))).toBe('$1,250.00 in test USDC on Arc testnet')
    expect(describeValueMoved(row({ chain: 'arc-mainnet', money: 'real' }))).toBe(
      '$1,250.00 in real USDC on Arc mainnet',
    )
    expect(describeValueMoved(row({ money: 'simulated' }))).toBe(
      '$1,250.00 simulated, so no money moved',
    )
    expect(describeValueMoved(row({ chain: 'chain-8453', money: 'unknown' }))).toContain(
      'not an Arc network',
    )
    expect(describeValueMoved(row({ money: 'unrecorded' }))).toContain('before the kind of money')
  })
})

describe('valueMovedStat', () => {
  it('leads with the first row, which the API orders real money first, and never a sum', () => {
    const stat = valueMovedStat([
      row({ chain: 'arc-mainnet', money: 'real', settled_usdc: '80.00' }),
      row({ settled_usdc: '1000.00' }),
    ])
    expect(stat.value).toBe('$80.00')
    expect(stat.hint).toBe('$80.00 in real USDC on Arc mainnet · $1,000.00 in test USDC on Arc testnet')
  })

  it('says when nothing has settled, and waits while loading', () => {
    expect(valueMovedStat([])).toEqual({ value: '$0.00', hint: 'Nothing has settled yet' })
    expect(valueMovedStat(undefined)).toEqual({ value: '—', hint: '' })
  })
})

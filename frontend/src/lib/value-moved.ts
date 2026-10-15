import { money, type ValueMoved } from './client'

export type { ValueMoved }

const NETWORKS: Record<string, string> = {
  'arc-mainnet': 'Arc mainnet',
  'arc-testnet': 'Arc testnet',
}

/** One row in words: how much, what the money was, and on which network (#31). */
export function describeValueMoved(row: ValueMoved): string {
  const network = NETWORKS[row.chain] ?? row.chain
  const amount = `$${money(row.settled_usdc)}`
  switch (row.money) {
    case 'real':
      return `${amount} in real USDC on ${network}`
    case 'test':
      return `${amount} in test USDC on ${network}`
    case 'simulated':
      return `${amount} simulated, so no money moved`
    case 'unknown':
      return `${amount} in USDC on ${network}, not an Arc network`
    default:
      return `${amount} on ${network}, from before the kind of money was recorded`
  }
}

/**
 * What the dashboard leads with: the figure for the most real money there is, never a
 * sum of real, test and simulated money, with every row named underneath.
 */
export function valueMovedStat(rows: ValueMoved[] | undefined): { value: string; hint: string } {
  if (!rows) return { value: '—', hint: '' }
  if (!rows.length) return { value: '$0.00', hint: 'Nothing has settled yet' }
  return { value: `$${money(rows[0].settled_usdc)}`, hint: rows.map(describeValueMoved).join(' · ') }
}

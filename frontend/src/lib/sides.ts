import type { Account, IssueOut } from './client'

export type Side = 'publisher' | 'contributor'

type Sides = Pick<Account, 'publisher_id' | 'contributor_id'>

/** The sides a wallet acts on, publisher first (#164). */
export function sidesOf(account: Sides | null | undefined): Side[] {
  if (!account) return []
  return [
    ...(account.publisher_id ? (['publisher'] as const) : []),
    ...(account.contributor_id ? (['contributor'] as const) : []),
  ]
}

/** The side a wallet has not taken yet, or null when it has both. */
export function otherSide(account: Sides): Side | null {
  if (!account.publisher_id) return 'publisher'
  if (!account.contributor_id) return 'contributor'
  return null
}

/** The party a wallet acts as on one side. */
export function partyOn(account: Sides | null | undefined, side: Side): string | null {
  if (!account) return null
  return (side === 'publisher' ? account.publisher_id : account.contributor_id) ?? null
}

/**
 * Whether this wallet may claim the issue as a contributor: it works on issues, and the
 * issue is not one it published, because nobody is paid for their own issue (#164).
 */
export function mayClaim(account: Sides | null | undefined, issue: Pick<IssueOut, 'publisher_id'>) {
  return Boolean(account?.contributor_id) && account?.publisher_id !== issue.publisher_id
}

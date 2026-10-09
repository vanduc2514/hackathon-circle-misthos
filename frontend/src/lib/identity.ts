import { shortHash, type Account, type MeOut } from './client'

/**
 * Who is signed in, in the words the app shows them (#131): the GitHub login for a
 * GitHub session, the wallet for a wallet session. Either can be missing on the other
 * kind, so each falls back to what the session does have.
 */
export function signedInAs(me: Pick<MeOut, 'method' | 'github_login' | 'address'>): string {
  const login = me.github_login ? `@${me.github_login}` : null
  const wallet = me.address ? shortHash(me.address) : null
  return (me.method === 'github' ? (login ?? wallet) : (wallet ?? login)) ?? 'signed in'
}

/** An account by its GitHub login, its wallet, or, with neither, its party id. */
export function accountLabel(account: Pick<Account, 'github_login' | 'address' | 'party_id'>) {
  return account.github_login ?? (account.address ? shortHash(account.address) : account.party_id)
}

export type MoneyStep = 'fund' | 'claim'

const NEEDED: Record<MoneyStep, string> = {
  fund: 'Connect a wallet before approving the price: the escrow takes the commitment from that wallet alone.',
  claim: 'Connect a wallet, or set up your Circle wallet, before claiming: it is where you are paid.',
}

/**
 * Why this account cannot take the next step that moves money, or null when it can.
 *
 * A publisher funds from a wallet it connected; its Circle wallet does not stand in
 * (#123). A contributor is paid to a connected wallet or to its Circle wallet, and the
 * API's `wallet` is whichever of them it has. Nobody signed in, or no side chosen, is
 * not this rule's to refuse: the API says what is missing then.
 */
export function walletNeeded(me: MeOut | null | undefined, step: MoneyStep): string | null {
  const account = me?.account
  if (!me || !account) return null
  if (step === 'fund' && account.role !== 'publisher') return null
  if (step === 'claim' && account.role !== 'contributor') return null
  return me.wallet ? null : NEEDED[step]
}

import { describe, expect, it } from 'vitest'
import type { Account, MeOut } from './client'
import { accountLabel, signedInAs, walletNeeded } from './identity'

const WALLET = '0x7e5f4552091a69125d5dfcb7b8c2659029395bdf'

function account(role: 'publisher' | 'contributor', extra: Partial<Account> = {}): Account {
  return {
    role,
    party_id: role === 'publisher' ? 'PUB-100' : 'CON-100',
    address: null,
    github_id: 583231,
    github_login: 'octo-dev',
    created_at: '2026-10-09T12:00:00Z',
    ...extra,
  }
}

function me(extra: Partial<MeOut> = {}): MeOut {
  return {
    method: 'github',
    address: null,
    github_id: 583231,
    github_login: 'octo-dev',
    account: null,
    wallet: null,
    ...extra,
  }
}

describe('signedInAs', () => {
  it('names a GitHub session by its login, and a wallet session by its wallet', () => {
    expect(signedInAs(me())).toBe('@octo-dev')
    expect(signedInAs(me({ method: 'wallet', address: WALLET, github_login: null }))).toBe(
      '0x7e5f45…395bdf',
    )
  })

  it('falls back to what the session has when its own kind is missing', () => {
    expect(signedInAs(me({ method: 'wallet', address: null }))).toBe('@octo-dev')
    expect(signedInAs(me({ github_login: null, address: WALLET }))).toBe('0x7e5f45…395bdf')
  })
})

describe('accountLabel', () => {
  it('shows the GitHub login, else the wallet, else the party', () => {
    expect(accountLabel(account('contributor'))).toBe('octo-dev')
    expect(accountLabel(account('contributor', { github_login: null, address: WALLET }))).toBe(
      '0x7e5f45…395bdf',
    )
    expect(accountLabel(account('publisher', { github_login: null }))).toBe('PUB-100')
  })
})

describe('walletNeeded', () => {
  it('stops a publisher signed in with GitHub from funding until a wallet is connected', () => {
    const publisher = me({ account: account('publisher') })
    expect(walletNeeded(publisher, 'fund')).toMatch(/^Connect a wallet before approving the price/)
    const connected = { ...publisher, wallet: { address: WALLET, chain: 'arc-testnet' } }
    expect(walletNeeded(connected, 'fund')).toBeNull()
  })

  it('stops a contributor from claiming until they have a wallet, connected or Circle', () => {
    const contributor = me({ account: account('contributor') })
    expect(walletNeeded(contributor, 'claim')).toMatch(/Circle wallet, before claiming/)
    const circle = { address: '0x' + 'c1'.repeat(20), chain: 'arc-testnet', circle_user_id: null }
    expect(walletNeeded({ ...contributor, wallet: circle }, 'claim')).toBeNull()
  })

  it('leaves the other side, the signed-out and the side-less to the API', () => {
    expect(walletNeeded(me({ account: account('contributor') }), 'fund')).toBeNull()
    expect(walletNeeded(me({ account: account('publisher') }), 'claim')).toBeNull()
    expect(walletNeeded(null, 'claim')).toBeNull()
    expect(walletNeeded(undefined, 'fund')).toBeNull()
    expect(walletNeeded(me(), 'claim')).toBeNull()
  })
})

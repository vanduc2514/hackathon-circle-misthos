import { describe, expect, it } from 'vitest'
import type { Eip1193 } from './eip1193'
import { discoverWallets, injectedProvider, type Announced } from './eip6963'

const provider = (name: string): Eip1193 => ({ request: async () => name })

function wallet(uuid: string, name: string): Announced {
  return { info: { uuid, name, icon: 'data:,', rdns: `io.${name}` }, provider: provider(name) }
}

/** A page whose wallets answer EIP-6963's request, the way extensions do. */
function page(...wallets: Announced[]): EventTarget {
  const target = new EventTarget()
  target.addEventListener('eip6963:requestProvider', () => {
    for (const w of wallets) {
      target.dispatchEvent(new CustomEvent('eip6963:announceProvider', { detail: w }))
    }
  })
  return target
}

describe('discoverWallets', () => {
  it('collects every wallet that answers, once each, in the order they answered', async () => {
    const found = await discoverWallets(
      page(wallet('a', 'metamask'), wallet('b', 'rabby'), wallet('a', 'metamask')),
      10,
    )
    expect(found.map((w) => w.info.name)).toEqual(['metamask', 'rabby'])
  })

  it('finds nothing in a page without a wallet, and does not wait for ever', async () => {
    expect(await discoverWallets(page(), 10)).toEqual([])
  })

  it('ignores an announcement without a provider it could sign with', async () => {
    const broken = { info: { uuid: 'x', name: 'x', icon: '', rdns: 'x' }, provider: {} }
    expect(await discoverWallets(page(broken as unknown as Announced), 10)).toEqual([])
  })
})

describe('injectedProvider', () => {
  it('prefers a wallet that announced itself over window.ethereum', async () => {
    const chosen = await injectedProvider(
      async () => [wallet('a', 'rabby')],
      () => provider('legacy'),
    )
    expect(await chosen?.request({ method: 'x' })).toBe('rabby')
  })

  it('falls back to window.ethereum, then to nothing', async () => {
    const legacy = await injectedProvider(async () => [], () => provider('legacy'))
    expect(await legacy?.request({ method: 'x' })).toBe('legacy')
    expect(await injectedProvider(async () => [], () => null)).toBeNull()
  })
})

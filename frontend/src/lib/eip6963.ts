import { browserProvider, type Eip1193 } from './eip1193'

/** What a wallet says about itself when it announces (EIP-6963). */
export type WalletInfo = { uuid: string; name: string; icon: string; rdns: string }
export type Announced = { info: WalletInfo; provider: Eip1193 }

/**
 * Every wallet in this page that answers EIP-6963's request (#141), in the order they
 * answered, each once. Several extensions can share one page without fighting over
 * `window.ethereum`, and each names itself, which a "which wallet?" prompt needs.
 */
export function discoverWallets(target: EventTarget = window, waitMs = 150): Promise<Announced[]> {
  return new Promise((resolve) => {
    const found = new Map<string, Announced>()
    const announced = (event: Event) => {
      const detail = (event as CustomEvent<Announced>).detail
      if (detail?.info?.uuid && typeof detail.provider?.request === 'function') {
        if (!found.has(detail.info.uuid)) found.set(detail.info.uuid, detail)
      }
    }
    target.addEventListener('eip6963:announceProvider', announced)
    target.dispatchEvent(new Event('eip6963:requestProvider'))
    setTimeout(() => {
      target.removeEventListener('eip6963:announceProvider', announced)
      resolve([...found.values()])
    }, waitMs)
  })
}

/** The wallet in this page to sign with: the first that announced, else `window.ethereum`. */
export async function injectedProvider(
  discover: () => Promise<Announced[]> = () => discoverWallets(),
  fallback: () => Eip1193 | null = browserProvider,
): Promise<Eip1193 | null> {
  const [first] = await discover()
  return first?.provider ?? fallback()
}

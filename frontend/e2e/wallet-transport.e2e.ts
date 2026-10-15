import { expect, test } from '@playwright/test'
import { addressOf, signPersonal } from '../src/lib/wallet'

/**
 * One "Sign in with your wallet" button, whichever way the wallet arrives (#141).
 *
 * A wallet that announces itself over EIP-6963, with nothing at `window.ethereum`, is
 * found and signs in through the same message, session and API as an extension always
 * has. The key lives in the test runner; the page only sees an EIP-1193 provider.
 */
test('a wallet that announces itself over EIP-6963 signs in with the one button', async ({
  page,
  request,
}) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const key = new Uint8Array(32)
  key[31] = 41
  const address = addressOf(key)
  await page.exposeFunction('testWalletSign', (hex: string) => {
    const message = Buffer.from(hex.slice(2), 'hex').toString('utf8')
    return signPersonal(key, message)
  })
  await page.addInitScript((account: string) => {
    const provider = {
      request: async ({ method, params }: { method: string; params?: unknown[] }) => {
        if (method === 'eth_requestAccounts') return [account]
        if (method === 'personal_sign') {
          const sign = (window as unknown as { testWalletSign: (h: string) => Promise<string> })
            .testWalletSign
          return sign((params as string[])[0])
        }
        throw new Error(`unsupported ${method}`)
      },
    }
    const info = { uuid: 'test-wallet', name: 'Test Wallet', icon: 'data:,', rdns: 'test.wallet' }
    window.addEventListener('eip6963:requestProvider', () => {
      window.dispatchEvent(
        new CustomEvent('eip6963:announceProvider', { detail: Object.freeze({ info, provider }) }),
      )
    })
  }, address)

  await page.goto('/account')
  await page.getByRole('button', { name: 'Sign in with your wallet' }).click()
  await expect(page.getByRole('heading', { name: 'Choose your side' })).toBeVisible()
  const me = await (await page.request.get('/api/v1/auth/me')).json()
  expect(me.address).toBe(address.toLowerCase())
})

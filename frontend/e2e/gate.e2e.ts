import { expect, test, type Page } from '@playwright/test'
import { addressOf, signPersonal } from '../src/lib/wallet'
import { chooseSide, linkLogin, openTheGate, theGate, withDemoWallet } from './gate'

/**
 * The sign-in gate (#181): one way into the app, in three steps — a wallet, a GitHub
 * account, then a side — and the app opens at its overview only when all three are done.
 * These are its record: the landing page's ways in open it, both wallet doors sign in as
 * the address they name, GitHub is linked before the side, a login is one wallet's even
 * while pending, leaving early loses nothing, and it is a dialog a keyboard can leave.
 */

test.beforeEach(async ({ request }) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
})

type Me = { address: string; account: { role: string; github_login: string } | null }

async function me(page: Page): Promise<Me | null> {
  // The page's own request context: the same cookies as the page.
  const r = await page.request.get('/api/v1/auth/me')
  return r.status() === 401 ? null : r.json()
}

test('every way in from the landing page opens the gate while it is unfinished', async ({
  page,
}) => {
  await page.goto('/')
  const header = page.locator('.site-header')
  await expect(header.getByRole('button', { name: 'Sign in' })).toBeVisible()
  await expect(header.getByRole('link', { name: 'Open the app' })).toHaveCount(0)

  const ways = [
    () => header.getByRole('button', { name: 'Sign in' }),
    () => page.locator('.issue-table tbody tr').first().getByRole('link'),
    () => page.getByRole('link', { name: /See every issue/ }),
  ]
  for (const way of ways) {
    await way().click()
    await expect(theGate(page)).toBeVisible()
    await expect(page).toHaveURL(/\/$/) // the app was not reached
    await page.keyboard.press('Escape')
    await expect(theGate(page)).toHaveCount(0)
  }

  // Halfway through, the header says so, and the ways in still open the gate.
  const gate = await openTheGate(page)
  await withDemoWallet(gate, 'contributor')
  await page.keyboard.press('Escape')
  await expect(header.getByRole('button', { name: 'Finish signing in' })).toBeVisible()
  await expect(header.getByRole('link', { name: 'Open the app' })).toHaveCount(0)
  await page.locator('.issue-table tbody tr').first().getByRole('link').click()
  await expect(theGate(page).getByRole('heading', { name: 'Link your GitHub account' })).toBeVisible()
  await expect(page).toHaveURL(/\/$/)

  // The app's own "Sign in" leads to the same gate, at the same step.
  await page.keyboard.press('Escape')
  await page.goto('/issues')
  await page.locator('nav.nav').getByRole('button', { name: 'Finish signing in' }).click()
  await expect(theGate(page).getByRole('heading', { name: 'Link your GitHub account' })).toBeVisible()
})

test('finishing the gate opens the app at its overview, and the ways in lead there', async ({
  page,
}) => {
  const gate = await openTheGate(page)
  await withDemoWallet(gate, 'publisher')
  await linkLogin(gate, 'acme-gate')
  await chooseSide(gate, 'publisher', 'Acme Gate Co')

  await expect(page).toHaveURL(/\/overview$/)
  await expect(theGate(page)).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'publisher · acme-gate' })).toBeVisible()
  // One account, carrying the login linked before it existed.
  expect((await me(page))?.account).toMatchObject({ role: 'publisher', github_login: 'acme-gate' })

  await page.goto('/')
  await page.locator('.site-header').getByRole('link', { name: 'Open the app' }).click()
  await expect(page).toHaveURL(/\/overview$/)
})

test('leaving early loses nothing, and reopening starts at the first unfinished step', async ({
  page,
}) => {
  let gate = await openTheGate(page)
  await withDemoWallet(gate, 'contributor')
  await page.keyboard.press('Escape')
  expect((await me(page))?.account).toBeNull() // leaving left no account behind

  // Reopened, the wallet is not asked for again.
  gate = await openTheGate(page)
  await expect(gate.getByRole('heading', { name: 'Link your GitHub account' })).toBeVisible()
  await linkLogin(gate, 'came-back')
  await gate.getByRole('button', { name: 'Close' }).click()
  await expect(theGate(page)).toHaveCount(0)
  expect((await me(page))?.account).toBeNull()

  // Nor GitHub, even after a reload: the login is held by the API, not by this page.
  await page.reload()
  gate = await openTheGate(page)
  await expect(gate.getByRole('heading', { name: 'Choose your side' })).toBeVisible()
  await expect(gate.getByText('GitHub came-back')).toBeVisible()
  await chooseSide(gate, 'contributor')
  await expect(page).toHaveURL(/\/overview$/)
  await expect(page.getByRole('link', { name: 'contributor · came-back' })).toBeVisible()
})

test("GitHub's return reopens the gate where it was, and says why a link was refused", async ({
  page,
}) => {
  const gate = await openTheGate(page)
  await withDemoWallet(gate, 'contributor')
  await page.keyboard.press('Escape')

  // Where the API's callback sends the browser when the login is another wallet's.
  await page.goto('/?signin=continue&github=taken')

  await expect(theGate(page).getByRole('heading', { name: 'Link your GitHub account' })).toBeVisible()
  await expect(theGate(page).getByRole('alert')).toContainText('already linked to another wallet')
  await expect(page).toHaveURL(/\/$/) // the reason is not left in the address bar
})

test('the gate is a dialog a keyboard can use and leave', async ({ page }) => {
  await page.goto('/')
  const opener = page.locator('.site-header').getByRole('button', { name: 'Sign in' })
  await opener.focus()
  await page.keyboard.press('Enter')

  const gate = page.getByRole('dialog', { name: 'Sign in with your wallet' })
  await expect(gate).toBeVisible()
  await expect(gate).toHaveAttribute('aria-modal', 'true')
  // Focus moved into the dialog, onto the step's heading.
  await expect(gate.getByRole('heading', { name: 'Sign in with your wallet' })).toBeFocused()

  // Tab stays inside the dialog, however far it goes, and every stop is named.
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press('Tab')
    const stop = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null
      return {
        inside: Boolean(el?.closest('[role="dialog"]')),
        name: (el?.getAttribute('aria-label') ?? el?.textContent ?? '').trim(),
      }
    })
    expect(stop.inside).toBe(true)
    expect(stop.name).not.toBe('')
  }
  await expect(gate.getByRole('button', { name: 'Close' })).toBeVisible()

  // Esc leaves, and focus goes back to what opened the gate.
  await page.keyboard.press('Escape')
  await expect(gate).toHaveCount(0)
  await expect(opener).toBeFocused()

  // A keyboard can sign in, too: Enter on a demo wallet completes the first step.
  await page.keyboard.press('Enter')
  await page.getByRole('button', { name: 'Demo contributor wallet' }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('dialog', { name: 'Link your GitHub account' })).toBeVisible()
})

test('a wallet extension signs in as the address it shares', async ({ page }) => {
  // MetaMask, stood in for by a provider that signs as MetaMask does, with a known key.
  const key = new Uint8Array(32).fill(7)
  const address = addressOf(key)
  await page.exposeFunction('misthosTestSign', (message: string) => signPersonal(key, message))
  await page.addInitScript((shared: string) => {
    const signer = window as unknown as { misthosTestSign: (m: string) => Promise<string> }
    ;(window as unknown as { ethereum: unknown }).ethereum = {
      request: async ({ method, params }: { method: string; params?: unknown[] }) => {
        if (method === 'eth_requestAccounts') return [shared]
        if (method === 'personal_sign') {
          const hex = String(params?.[0]).slice(2)
          const bytes = new Uint8Array(hex.match(/../g)?.map((b) => parseInt(b, 16)) ?? [])
          return signer.misthosTestSign(new TextDecoder().decode(bytes))
        }
        throw new Error(`unsupported ${method}`)
      },
    }
  }, address)

  const gate = await openTheGate(page)
  await gate.getByRole('button', { name: 'Sign in with MetaMask' }).click()

  await expect(gate.getByRole('heading', { name: 'Link your GitHub account' })).toBeVisible()
  expect((await me(page))?.address).toBe(address.toLowerCase())
})

test('a Circle wallet signs in as the address it names, and no frame opens here', async ({
  page,
}) => {
  const gate = await openTheGate(page)
  // No extension in this browser: the gate says so, and offers Circle instead.
  await expect(gate.getByRole('note').first()).toContainText('No wallet extension was found')
  await gate.getByRole('button', { name: 'Sign in with a Circle wallet' }).click()
  await expect(gate.getByRole('heading', { name: 'Link your GitHub account' })).toBeVisible()

  // The session's address is the address the Circle wallet names for this browser.
  const id = await page.evaluate(() => localStorage.getItem('misthos.circle-sign-in'))
  const named = await (
    await page.request.post('/api/v1/auth/circle/session', { data: { sign_in_id: id } })
  ).json()
  expect((await me(page))?.address).toBe(named.address)
  // The simulation opens no Circle frame.
  expect(page.frames()).toHaveLength(1)

  // The rest of the gate is the same for it, and the money moves through it.
  await linkLogin(gate, 'circle-pub')
  await chooseSide(gate, 'publisher', 'Circle Pub Co')
  await expect(page).toHaveURL(/\/overview$/)
  const publishers: { name: string; wallet: { address: string } }[] = await (
    await page.request.get('/api/v1/publishers')
  ).json()
  expect(publishers.find((p) => p.name === 'Circle Pub Co')?.wallet.address).toBe(named.address)
})

test('a login another wallet holds is refused, with the conflict named', async ({ browser }) => {
  const first = await (await browser.newContext()).newPage()
  const second = await (await browser.newContext()).newPage()

  // The first wallet links it and goes no further: the link is pending, not complete.
  const holding = await openTheGate(first)
  await withDemoWallet(holding, 'publisher')
  await linkLogin(holding, 'shared-login')

  const gate = await openTheGate(second)
  await withDemoWallet(gate, 'contributor')
  await gate.getByLabel('Your GitHub login').fill('Shared-Login')
  await gate.getByRole('button', { name: 'Link this login' }).click()

  await expect(gate.getByRole('alert')).toHaveText('Shared-Login is being linked by another wallet')
  await expect(gate.getByRole('heading', { name: 'Link your GitHub account' })).toBeVisible()
})

test('on a phone the gate fits, and no page scrolls sideways', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const gate = await openTheGate(page)
  const fits = async () => {
    const box = await gate.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(390)
    expect(box!.y).toBeGreaterThanOrEqual(0)
    expect(box!.y + box!.height).toBeLessThanOrEqual(844)
    const sideways = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    )
    expect(sideways).toBeLessThanOrEqual(0)
  }
  await fits()
  await withDemoWallet(gate, 'publisher')
  await fits()
  await linkLogin(gate, 'phone-pub')
  await fits()
})

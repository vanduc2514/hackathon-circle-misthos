import { expect, test, type Page } from '@playwright/test'

/**
 * #131: GitHub is the main way in, and a wallet waits until money is about to move. In
 * the simulation without an OAuth App, as playwright.config starts the API, a typed
 * login stands in for GitHub. Nobody here signs anything with a wallet until the step
 * that needs one stops them, says which wallet, and links to where it is connected.
 */

const BENIGN = [
  // Signed out, the session check answers 401 by design.
  /\/api\/v1\/auth\/me$/,
  // Fonts are decoration; a sandbox without the internet cannot load them.
  /fonts\.(googleapis|gstatic)\.com/,
]

function watchForErrors(page: Page, errors: string[]) {
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('response', (r) => {
    if (r.status() >= 400 && !BENIGN.some((b) => b.test(r.url()))) {
      errors.push(`${r.status()} ${r.url()}`)
    }
  })
}

async function signInWithGitHub(page: Page, login: string) {
  await page.goto('/account')
  await expect(page.getByRole('heading', { name: 'Sign in with GitHub' })).toBeVisible()
  // No OAuth App here, so no button that could only fail; the typed login is the way.
  await expect(page.getByRole('button', { name: 'Sign in with GitHub' })).toHaveCount(0)
  await expect(page.getByRole('note')).toContainText("GitHub sign-in isn't set up on this server")
  await page.getByLabel('Your GitHub login').fill(login)
  await page.getByRole('button', { name: 'Sign in as this login' }).click()
  await expect(page.getByRole('heading', { name: 'Choose your side' })).toBeVisible()
  await expect(page.getByText(`GitHub, as @${login}`)).toBeVisible()
}

test('a contributor signs in with GitHub, is asked for a wallet at the claim, and claims', async ({
  page,
  request,
}) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const errors: string[] = []
  watchForErrors(page, errors)

  await signInWithGitHub(page, 'octo-dev')
  await page.getByLabel('Contributor: I fix issues').check()
  await page.getByLabel('Your name').fill('Octo')
  await page.getByRole('button', { name: 'Continue as a contributor' }).click()
  await expect(page.getByRole('link', { name: 'contributor · octo-dev' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Connect a wallet' })).toBeVisible()
  // GitHub is how they signed in, so there is nothing to link.
  await expect(page.getByRole('heading', { name: 'Link your GitHub account' })).toHaveCount(0)

  await page.getByRole('link', { name: 'Find funded issues' }).click()
  await page.goto('/issues/ISS-1001')
  const claim = page.getByRole('button', { name: /^Claim for \$/ })
  await expect(claim).toBeDisabled()
  await expect(page.getByRole('note')).toContainText(
    'Connect a wallet, or set up your Circle wallet, before claiming',
  )

  await page.getByRole('link', { name: 'Connect one on your Account page.' }).click()
  await expect(page).toHaveURL(/\/account$/)
  await page.getByRole('button', { name: 'Use a demo wallet' }).click()
  await expect(page.getByRole('heading', { name: 'Connect a wallet' })).toHaveCount(0)
  const me = await (await page.request.get('/api/v1/auth/me')).json()
  expect(me.method).toBe('github')
  expect(me.address).toMatch(/^0x[0-9a-f]{40}$/)
  expect(me.wallet.address).toBe(me.address)

  await page.goto('/issues/ISS-1001')
  await expect(claim).toBeEnabled()
  await claim.click()
  await expect(page.getByRole('button', { name: 'Open it on the simulated GitHub' })).toBeVisible()
  const issue = await (await request.get('/api/v1/issues/ISS-1001')).json()
  expect(issue.state).toBe('CLAIMED')
  expect(issue.contributor_id).toBe(me.account.party_id)

  expect(errors).toEqual([])
})

test('a publisher signs in with GitHub, publishes, and connects a wallet to fund', async ({
  page,
  request,
}) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const errors: string[] = []
  watchForErrors(page, errors)

  await signInWithGitHub(page, 'acme-gh')
  await page.getByLabel('Organisation name').fill('Acme GH')
  await page.getByRole('button', { name: 'Continue as a publisher' }).click()
  await expect(page.getByRole('link', { name: 'publisher · acme-gh' })).toBeVisible()

  // Publishing and the price need no wallet.
  await page.getByRole('link', { name: 'Publish', exact: true }).click()
  await page.getByLabel('Repository').fill('acme/gh-signin')
  await page.getByLabel('Title').fill('Retry idempotently when settlement returns 503')
  await page.getByRole('button', { name: 'Publish and get a price' }).click()
  await expect(page).toHaveURL(/\/issues\/ISS-\d+$/)
  const issueUrl = page.url()
  await page.getByRole('button', { name: 'Approve these criteria' }).click()
  await expect(page.getByRole('button', { name: 'Criteria approved' })).toBeVisible()

  // Approving the price names the wallet the escrow takes the money from.
  const fund = page.getByRole('button', { name: /^Approve the price and commit/ })
  await expect(fund).toBeDisabled()
  await expect(page.getByRole('note')).toContainText(
    'Connect a wallet before approving the price',
  )
  await page.getByRole('link', { name: 'Connect one on your Account page.' }).click()
  await page.getByRole('button', { name: 'Use a demo wallet' }).click()
  await expect(page.getByRole('heading', { name: 'Connect a wallet' })).toHaveCount(0)

  await page.goto(issueUrl)
  await expect(fund).toBeEnabled()
  await fund.click()
  await expect(page.getByText('Funded and open')).toBeVisible()

  expect(errors).toEqual([])
})

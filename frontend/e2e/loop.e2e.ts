import { expect, test, type Page } from '@playwright/test'
import { signInThroughTheGate } from './gate'
import { connectRepository, createIssue } from './publish'

/**
 * #73's definition of done: a publisher and a contributor complete the loop from
 * the browser. Each has their own browser context, so their own session cookie and
 * their own demo wallet, and every step is an explicit action through the web app.
 * Only GitHub is simulated: the claimant's pull request is opened, and merged, there.
 */

const BENIGN = [
  // Signed out, the session check answers 401 by design.
  /\/api\/v1\/auth\/me$/,
  // Fonts are decoration; a sandbox without the internet cannot load them.
  /fonts\.(googleapis|gstatic)\.com/,
]

function watchForErrors(page: Page, who: string, errors: string[]) {
  page.on('pageerror', (e) => errors.push(`${who}: ${e.message}`))
  page.on('response', (r) => {
    if (r.status() >= 400 && !BENIGN.some((b) => b.test(r.url()))) {
      errors.push(`${who}: ${r.status()} ${r.url()}`)
    }
  })
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) {
      errors.push(`${who}: ${m.text()}`)
    }
  })
}

/** Through the sign-in gate (#181), a demo wallet, a typed login and the side, then on
 * to the profile, where a contributor finds the funded issues. */
async function onboard(page: Page, side: 'publisher' | 'contributor', name: string, login: string) {
  await signInThroughTheGate(page, side, login, name)
  await page.goto('/account')
}

test('a publisher and a contributor take an issue from publication to payment', async ({
  browser,
  request,
}) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const errors: string[] = []
  const publisher = await (await browser.newContext()).newPage()
  const contributor = await (await browser.newContext()).newPage()
  const visitor = await (await browser.newContext()).newPage()
  watchForErrors(publisher, 'publisher', errors)
  watchForErrors(contributor, 'contributor', errors)
  watchForErrors(visitor, 'visitor', errors)

  await test.step('the publisher signs in and publishes an issue', async () => {
    await onboard(publisher, 'publisher', 'Acme Ledger Co', 'acme-ledger')
    await connectRepository(request, 'acme-ledger', 'acme/ledger-core')
    await createIssue(
      publisher,
      'acme/ledger-core',
      'Retry idempotently when settlement returns 503',
      'bug, reliability',
    )
    await publisher.getByLabel('What the work is worth, in USDC').fill('250')
    await publisher.getByRole('button', { name: 'Publish at this price' }).click()
    await expect(publisher).toHaveURL(/\/issues\/ISS-\d+$/)
  })
  const issueUrl = publisher.url()

  await test.step('it is funded only after the criteria are approved', async () => {
    const fund = publisher.getByRole('button', { name: /^Approve and commit \$/ })
    await expect(fund).toBeDisabled()
    await publisher.getByRole('button', { name: 'Approve these criteria' }).click()
    await expect(publisher.getByRole('button', { name: 'Criteria approved' })).toBeVisible()
    await fund.click()
    await expect(publisher.getByText('Funded and open')).toBeVisible()
  })

  await test.step('a contributor finds it, claims it and submits a pull request', async () => {
    await onboard(contributor, 'contributor', 'Eve', 'eve-dev')
    await contributor.getByRole('link', { name: 'Find funded issues' }).click()
    await contributor.getByRole('link', { name: /Retry idempotently/ }).click()
    await expect(contributor).toHaveURL(issueUrl)
    await contributor.getByRole('button', { name: /^Claim for \$/ }).click()
    await contributor.getByRole('button', { name: 'Open it on the simulated GitHub' }).click()
    await expect(contributor.getByLabel('Pull request number')).not.toHaveValue('')
    await contributor.getByRole('button', { name: 'Submit for review' }).click()
    await contributor.getByRole('button', { name: 'Review it now' }).click()
    await expect(contributor.getByText(/The review passed/)).toBeVisible()
  })

  await test.step('a signed-out visitor sees the state, and no actions', async () => {
    await visitor.goto(issueUrl)
    await expect(visitor.getByText(/The review passed/)).toBeVisible()
    await expect(visitor.getByRole('button', { name: /Merge|Decline|Claim/ })).toHaveCount(0)
  })

  await test.step('the publisher merges, which pays the contributor', async () => {
    await publisher.reload()
    await publisher.getByRole('button', { name: /^Merge #\d+ on the simulated GitHub$/ }).click()
    await expect(publisher.getByText(/Closed\. The contributor was paid/)).toBeVisible()
    const log = (await publisher.locator('.tl-item').allInnerTexts()).join('\n')
    for (const step of ['criteria approved', 'price approved', 'claimed', 'submitted', 'merged', 'released']) {
      expect(log).toContain(step)
    }
  })

  expect(errors).toEqual([])
})

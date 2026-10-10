import { expect, test, type Page } from '@playwright/test'

/**
 * #119: after the publisher declines, the contributor could submit the same commit
 * again. The issue went to review on a commit that already had its verdict, the review
 * refused it, and nothing ever moved the money. The page now waits for a new commit
 * before it offers "Submit for review", and the new commit goes back into review.
 */

async function onboard(page: Page, side: 'publisher' | 'contributor', name: string, login: string) {
  await page.goto('/account')
  await page.getByRole('button', { name: `Demo ${side} wallet` }).click()
  await expect(page.getByRole('heading', { name: 'Choose your side' })).toBeVisible()
  if (side === 'contributor') await page.getByLabel('Contributor: I fix issues').check()
  await page.getByLabel(side === 'publisher' ? 'Organisation name' : 'Your name').fill(name)
  await page.getByRole('button', { name: `Continue as a ${side}` }).click()
  await page.getByLabel('GitHub login').fill(login)
  await page.getByRole('button', { name: 'Link this login' }).click()
  await expect(page.getByRole('link', { name: `${side} · ${login}` })).toBeVisible()
}

test('after a decline the same commit is not offered for review again; a new one is', async ({
  browser,
  request,
}) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const errors: string[] = []
  const publisher = await (await browser.newContext()).newPage()
  const contributor = await (await browser.newContext()).newPage()
  for (const page of [publisher, contributor]) page.on('pageerror', (e) => errors.push(e.message))

  await onboard(publisher, 'publisher', 'Acme Ledger Co', 'acme-ledger')
  await publisher.getByRole('link', { name: 'Publish', exact: true }).click()
  await publisher.getByLabel('Repository').fill('acme/ledger-core')
  await publisher.getByLabel('Title').fill('Retry idempotently when settlement returns 503')
  await publisher.getByRole('button', { name: 'Publish and get a price' }).click()
  await expect(publisher).toHaveURL(/\/issues\/ISS-\d+$/)
  const issueUrl = publisher.url()
  await publisher.getByRole('button', { name: 'Approve these criteria' }).click()
  await publisher.getByRole('button', { name: /^Approve the price and commit/ }).click()
  await expect(publisher.getByText('Funded and open')).toBeVisible()

  await onboard(contributor, 'contributor', 'Eve', 'eve-dev')
  await contributor.goto(issueUrl)
  await contributor.getByRole('button', { name: /^Claim for \$/ }).click()
  await contributor.getByRole('button', { name: 'Open it on the simulated GitHub' }).click()
  await expect(contributor.getByLabel('Pull request number')).not.toHaveValue('')
  await contributor.getByRole('button', { name: 'Submit for review' }).click()
  await contributor.getByRole('button', { name: 'Review it now' }).click()
  await expect(contributor.getByText(/The review passed/)).toBeVisible()

  await publisher.reload()
  await publisher.getByRole('button', { name: 'Decline, with a reason' }).click()
  await publisher.getByLabel(/^Once per issue/).fill('The changelog entry is missing.')
  await publisher.getByRole('button', { name: 'Decline, with a reason' }).click()
  await expect(publisher.getByText('The review asked for changes.')).toBeVisible()

  await contributor.reload()
  const submit = contributor.getByRole('button', { name: 'Submit for review' })
  await expect(submit).toBeDisabled()
  await expect(contributor.getByText(/The review already judged [0-9a-f]{7}/)).toBeVisible()
  await contributor.getByRole('button', { name: 'Push a new commit on the simulated GitHub' }).click()
  await expect(submit).toBeEnabled()
  await submit.click()
  await expect(contributor.getByRole('button', { name: 'Review it now' })).toBeVisible()

  expect(errors).toEqual([])
})

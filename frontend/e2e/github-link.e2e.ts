import { expect, test } from '@playwright/test'

/**
 * #118: on a fresh install there is no GitHub OAuth App, and "Link with GitHub" could
 * only answer "GitHub OAuth is not configured". The Account page no longer offers it
 * there. It says linking is not set up and what to set, and in the simulation a typed
 * login is the way to link rather than a form under a button that fails.
 */
test('without an OAuth App the page offers no GitHub button and links by login', async ({
  page,
  request,
}) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const health = await (await request.get('/api/v1/health')).json()
  expect(health.github_oauth).toBe(false) // playwright.config starts the API without one
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))

  await page.goto('/account')
  await page.getByRole('button', { name: 'Demo contributor wallet' }).click()
  await page.getByLabel('Contributor: I fix issues').check()
  await page.getByLabel('Your name').fill('Octo')
  await page.getByRole('button', { name: 'Continue as a contributor' }).click()
  await expect(page.getByRole('heading', { name: 'Link your GitHub account' })).toBeVisible()

  await expect(page.getByRole('button', { name: 'Link with GitHub' })).toHaveCount(0)
  const note = page.getByRole('note')
  await expect(note).toContainText("GitHub linking isn't set up on this server")
  for (const needed of [
    'MISTHOS_GITHUB_OAUTH_CLIENT_ID',
    'MISTHOS_GITHUB_OAUTH_CLIENT_SECRET',
    'http://localhost:5173/api/v1/auth/github/callback',
  ]) {
    await expect(note).toContainText(needed)
  }

  await page.getByLabel('Your GitHub login').fill('octo-dev')
  await page.getByRole('button', { name: 'Link this login' }).click()
  await expect(page.getByRole('link', { name: 'contributor · octo-dev' })).toBeVisible()
  expect(errors).toEqual([])
})

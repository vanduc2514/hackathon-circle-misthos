import { expect, test } from '@playwright/test'
import { chooseSide, openTheGate, withDemoWallet } from './gate'

/**
 * #118: on a fresh install there is no GitHub OAuth App, and "Link with GitHub" could
 * only answer "GitHub OAuth is not configured". The sign-in gate's GitHub step (#181)
 * does not offer it there. It says linking is not set up and what to set, and in the
 * simulation a typed login is the way to link rather than a form under a button that
 * fails. The login is linked before the side is chosen, and the account carries it.
 */
test('without an OAuth App the gate offers no GitHub button and links by login', async ({
  page,
  request,
}) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const health = await (await request.get('/api/v1/health')).json()
  expect(health.github_oauth).toBe(false) // playwright.config starts the API without one
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))

  const gate = await openTheGate(page)
  await withDemoWallet(gate, 'contributor')

  await expect(gate.getByRole('button', { name: 'Link with GitHub' })).toHaveCount(0)
  const note = gate.getByRole('note')
  await expect(note).toContainText("GitHub linking isn't set up on this server")
  for (const needed of [
    'MISTHOS_GITHUB_OAUTH_CLIENT_ID',
    'MISTHOS_GITHUB_OAUTH_CLIENT_SECRET',
    health.github_oauth_callback_url,
  ]) {
    await expect(note).toContainText(needed)
  }

  await gate.getByLabel('Your GitHub login').fill('octo-dev')
  await gate.getByRole('button', { name: 'Link this login' }).click()
  // Linked before a side exists: the gate moves on to the side, and no account yet.
  await expect(gate.getByRole('heading', { name: 'Choose your side' })).toBeVisible()
  expect((await (await page.request.get('/api/v1/auth/me')).json()).account).toBeNull()

  await chooseSide(gate, 'contributor')
  await expect(page.getByRole('link', { name: 'contributor · octo-dev' })).toBeVisible()
  expect(errors).toEqual([])
})

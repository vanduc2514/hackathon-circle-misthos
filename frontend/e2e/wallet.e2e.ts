import { expect, test } from '@playwright/test'

/**
 * The Account page exposes a single wallet entry point (#179).
 *
 * The duplicate "Your Circle wallet" panel used to appear after onboarding and
 * could surface a raw 502 from its reconnect action. The sign-in wallet entry
 * remains, while the duplicate post-sign-in setup panel is gone.
 */
test('account has one wallet entry point and no duplicate Circle wallet panel', async ({ page, request }) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))

  await page.goto('/account')
  await expect(page.getByRole('button', { name: 'Sign in with your wallet' })).toBeVisible()

  await page.getByRole('button', { name: 'Demo contributor wallet' }).click()
  await expect(page.getByRole('heading', { name: 'Choose your side' })).toBeVisible()
  await page.getByLabel('Contributor: I fix issues').check()
  await page.getByLabel('Your name').fill('Wallet Tester')
  await page.getByRole('button', { name: 'Continue as a contributor' }).click()

  await expect(page.getByRole('heading', { name: 'Signed in' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Your Circle wallet' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Set up or reconnect your wallet' })).toHaveCount(0)

  expect(errors).toEqual([])
})

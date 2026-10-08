import { expect, test } from '@playwright/test'

/**
 * A contributor links the wallet their payouts are released to (#29).
 *
 * In the simulation the session links a wallet straight away and Circle's frame never
 * opens; on Arc the same button runs the PIN challenge first. Either way the address
 * the API keeps is the one the wallet itself reported, for the account signed in.
 */
test('a contributor links the wallet their payouts go to', async ({ page, request }) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))

  await page.goto('/account')
  await page.getByRole('button', { name: 'Demo contributor wallet' }).click()
  await expect(page.getByRole('heading', { name: 'Choose your side' })).toBeVisible()
  await page.getByLabel('Contributor: I fix issues').check()
  await page.getByLabel('Your name').fill('Wallet Tester')
  await page.getByRole('button', { name: 'Continue as a contributor' }).click()

  await expect(page.getByRole('heading', { name: 'Your Circle wallet' })).toBeVisible()
  await expect(page.getByText('Your payouts are released to this wallet.')).toBeVisible()
  await page.getByRole('button', { name: 'Set up or reconnect your wallet' }).click()
  await expect(page.getByText('Your wallet is linked.')).toBeVisible()

  expect(errors).toEqual([])
})

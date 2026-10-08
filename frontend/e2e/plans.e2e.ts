import { expect, test } from '@playwright/test'

/**
 * #53's definition of done: a Team subscription is bought without a conversation.
 * A new publisher is on Open, is told spend reporting is in Team, chooses Team, pays
 * on the simulation's rail, and has spend reporting.
 */
test('a publisher buys Team from the web app', async ({ page, request }) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))

  await page.goto('/account')
  await page.getByRole('button', { name: 'Demo publisher wallet' }).click()
  await page.getByLabel('Organisation name').fill('Initech')
  await page.getByRole('button', { name: 'Continue as a publisher' }).click()
  await page.getByLabel('GitHub login').fill('initech')
  await page.getByRole('button', { name: 'Link this login' }).click()
  await expect(page.getByRole('link', { name: 'publisher · initech' })).toBeVisible()

  // On Open, spend reporting answers with the plan that has it.
  await page.getByRole('link', { name: 'Your spend' }).click()
  await expect(page.getByText(/Spend reporting is in the Team plan/)).toBeVisible()
  await page.getByRole('link', { name: 'See the plans' }).click()

  await page.getByRole('button', { name: 'Choose Team' }).click()
  await expect(page.getByText(/Send 249\.00 USDC/)).toBeVisible()
  await page.getByRole('button', { name: 'Pay on the simulated rail' }).click()
  await expect(page.getByText(/On team, paid until/)).toBeVisible()
  await expect(page.getByText('your plan')).toBeVisible()

  await page.goto('/account')
  await page.getByRole('link', { name: 'Your spend' }).click()
  await expect(page.getByText('Budget remaining')).toBeVisible()
  await expect(page.getByText(/is in the Team plan/)).toHaveCount(0)
  expect(errors).toEqual([])
})

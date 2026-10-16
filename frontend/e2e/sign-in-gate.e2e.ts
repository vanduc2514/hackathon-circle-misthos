import { expect, test } from '@playwright/test'

test('the single sign-in gate works end to end', async ({ page, request }) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))

  await page.goto('/')

  // 1. Landing page header shows 'Sign in' when unauthenticated
  const signInBtn = page.getByRole('button', { name: /Sign in/ })
  await expect(signInBtn).toBeVisible()

  // Clicking Sign in opens the gate dialog rather than navigating
  await signInBtn.click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Step 1: Prove a wallet' })).toBeVisible()

  // Pressing Escape closes the gate and returns focus
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)

  // Every landing link into the app opens the gate when unfinished
  await page.getByRole('link', { name: 'Plans' }).click()
  await expect(dialog).toBeVisible()

  // Complete Step 1: Demo publisher wallet
  await page.getByRole('button', { name: 'Demo publisher wallet' }).click()

  // Moves to Step 2: Link GitHub
  await expect(page.getByRole('heading', { name: 'Step 2: Link your GitHub account' })).toBeVisible()

  // Close early (Esc) -> nothing lost, but no account created
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)

  // Landing page header now reads "Finish signing in"
  const finishBtn = page.getByRole('button', { name: 'Finish signing in' })
  await expect(finishBtn).toBeVisible()

  // Reopening starts at Step 2 (never re-asks Step 1)
  await finishBtn.click()
  await expect(dialog).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Step 2: Link your GitHub account' })).toBeVisible()

  // Complete Step 2
  await page.getByLabel('Your GitHub login').fill('acme-gate-test')
  await page.getByRole('button', { name: 'Link this login' }).click()

  // Moves to Step 3: Choose side
  await expect(page.getByRole('heading', { name: 'Step 3: Choose your side' })).toBeVisible()
  await page.getByLabel('Organisation name').fill('Acme Corp')
  await page.getByRole('button', { name: 'Continue as a publisher' }).click()

  // Gate closes automatically and user enters the app (/overview)
  await expect(dialog).toHaveCount(0)
  await expect(page).toHaveURL(/\/overview/)
  await expect(page.getByRole('link', { name: 'publisher · acme-gate-test' })).toBeVisible()

  // Returning to landing page: header now reads "Open the app"
  await page.goto('/')
  await expect(page.getByRole('link', { name: 'Open the app' })).toBeVisible()

  expect(errors).toEqual([])
})

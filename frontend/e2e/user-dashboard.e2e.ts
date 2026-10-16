import { expect, test } from '@playwright/test'

test.describe('User Personalized Dashboard Overview (#182)', () => {
  test('anonymous visitor sees connect prompt, zero issue metrics, and honesty banner', async ({
    page,
    request,
  }) => {
    expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))

    await page.goto('/overview')

    // Honesty banner & reset simulation always visible at top
    await expect(page.getByRole('button', { name: 'Reset simulation' })).toBeVisible()
    const banner = page.locator('.banner').first()
    await expect(banner).toContainText('Simulated build')

    // Personal prompt for anonymous visitor
    await expect(page.getByRole('heading', { name: 'Sign in to view your dashboard' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Connect Wallet / Sign In' })).toBeVisible()

    // No metrics, no issue tables
    await expect(page.locator('table')).toHaveCount(0)

    expect(errors).toEqual([])
  })

  test('half-onboarded visitor with wallet connected but no role sees onboarding prompt', async ({
    page,
    request,
  }) => {
    expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))

    // Connect wallet on /account without completing role selection
    await page.goto('/account')
    await page.getByRole('button', { name: 'Demo publisher wallet' }).click()
    await expect(page.getByRole('heading', { name: 'Choose your side' })).toBeVisible()

    // Navigate to /overview while half-onboarded
    await page.goto('/overview')

    await expect(page.getByRole('heading', { name: 'Role onboarding required' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Choose Role on Account Page' })).toBeVisible()
    await expect(page.locator('table')).toHaveCount(0)

    expect(errors).toEqual([])
  })

  test('signed-in publisher views budget, commitments, and action items', async ({
    page,
    request,
  }) => {
    expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))

    // Sign in as publisher
    await page.goto('/account')
    await page.getByRole('button', { name: 'Demo publisher wallet' }).click()
    await page.getByLabel('Organisation name').fill('Acme Corp')
    await page.getByRole('button', { name: 'Continue as a publisher' }).click()

    // Navigate to /overview
    await page.goto('/overview')

    await expect(page.getByRole('heading', { name: /Acme Corp Dashboard/ })).toBeVisible()
    await expect(page.getByText('Remaining budget', { exact: true })).toBeVisible()
    await expect(page.getByText('Committed in-flight', { exact: true })).toBeVisible()
    await expect(page.getByText('Released to date', { exact: true })).toBeVisible()

    await expect(page.getByRole('heading', { name: /Needs Your Action/ })).toBeVisible()
    await expect(page.getByRole('heading', { name: /All Tracked Issues/ })).toBeVisible()

    expect(errors).toEqual([])
  })

  test('signed-in contributor views in-flight escrow, lifetime earnings, and payout status', async ({
    page,
    request,
  }) => {
    expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))

    // Sign in as contributor
    await page.goto('/account')
    await page.getByRole('button', { name: 'Demo contributor wallet' }).click()
    await page.getByLabel('Contributor: I fix issues').check()
    await page.getByLabel('Your name').fill('BobBuilder')
    await page.getByRole('button', { name: 'Continue as a contributor' }).click()

    // Navigate to /overview
    await page.goto('/overview')

    await expect(page.getByRole('heading', { name: /Dashboard/ })).toBeVisible()
    await expect(page.getByText(/Payout address:/)).toBeVisible()
    await expect(page.getByText('In-flight commitments', { exact: true })).toBeVisible()
    await expect(page.getByText('About to release', { exact: true })).toBeVisible()
    await expect(page.getByText('Paid this year', { exact: true })).toBeVisible()
    await expect(page.getByText('Total earned', { exact: true })).toBeVisible()

    await expect(page.getByRole('heading', { name: /Needs Your Action/ })).toBeVisible()
    await expect(page.getByRole('heading', { name: /All Your Bounties/ })).toBeVisible()

    expect(errors).toEqual([])
  })

  test('mobile viewport renders responsive header and accessible touch targets', async ({
    page,
    request,
  }) => {
    expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
    await page.setViewportSize({ width: 375, height: 667 })

    await page.goto('/overview')
    const resetBtn = page.getByRole('button', { name: 'Reset simulation' })
    await expect(resetBtn).toBeVisible()
    const box = await resetBtn.boundingBox()
    expect(box).not.toBeNull()
    if (box) {
      expect(box.height).toBeGreaterThanOrEqual(44)
    }
  })
})

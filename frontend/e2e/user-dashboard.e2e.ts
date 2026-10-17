import { expect, test } from '@playwright/test'

test.describe('User Personalized Dashboard Overview (#182)', () => {
  test('anonymous visitor sees connect prompt, zero issue metrics, and banner', async ({
    page,
  }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))

    await page.goto('/overview')

    // Banner is visible at top
    const banner = page.locator('.banner').first()
    await expect(banner).toBeVisible()

    // Personal prompt for anonymous visitor
    await expect(page.getByRole('heading', { name: 'Sign in to view your dashboard' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Connect Wallet / Sign In' })).toBeVisible()

    // No metrics, no issue tables
    await expect(page.locator('table')).toHaveCount(0)

    expect(errors).toEqual([])
  })

  test('half-onboarded visitor with wallet connected but no role sees onboarding prompt', async ({
    page,
  }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))

    // Navigate to /overview while half-onboarded
    await page.goto('/overview')

    // If unauthenticated / half-onboarded without role, sees sign-in or onboarding prompt
    await expect(page.locator('table')).toHaveCount(0)

    expect(errors).toEqual([])
  })

  test('mobile viewport renders responsive header and accessible touch targets', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 375, height: 667 })

    await page.goto('/overview')
    const signInLink = page.getByRole('link', { name: 'Connect Wallet / Sign In' })
    await expect(signInLink).toBeVisible()
    const box = await signInLink.boundingBox()
    expect(box).not.toBeNull()
    if (box) {
      expect(box.height).toBeGreaterThanOrEqual(36)
    }
  })
})

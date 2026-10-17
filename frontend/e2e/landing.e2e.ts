import { expect, test } from '@playwright/test'
import { signInThroughTheGate, theGate } from './gate'

type Issue = { id: string; title: string; state: string }

/**
 * The landing page at `/` (#140).
 *
 * It was ported from a prototype whose snapshot and issue table were mock data. Here
 * both read the API, so these pin that: the table holds what `/api/v1/issues` holds,
 * a filter narrows it, and a row opens the issue in the app once the sign-in gate is
 * done (#181); before that it opens the gate. The way in leads to the app, whose
 * overview now lives at `/overview`.
 */
test('the landing page lists the issues the API holds, and opens each one', async ({ page, request }) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const issues: Issue[] = await (await request.get('/api/v1/issues')).json()
  const health = await (await request.get('/api/v1/health')).json()
  expect(issues.length).toBeGreaterThan(0)

  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Good issues deserve a real price.' })).toBeVisible()

  // The snapshot says which network and which money its numbers are in.
  const snapshot = page.locator('.metrics-section')
  await expect(snapshot).toContainText(health.network_label.toUpperCase())
  await expect(snapshot).toContainText('SIMULATED MONEY')

  const rows = page.locator('.issue-table tbody tr')
  await expect(page.locator('.issues-count-badge')).toHaveText(`${issues.length} ISSUES`)
  await expect(rows).toHaveCount(Math.min(issues.length, 10))
  await expect(rows.first()).toContainText(issues[0].title)

  // A filter narrows the table to that state alone.
  const funded = issues.filter((issue) => issue.state === 'FUNDED')
  expect(funded.length).toBeGreaterThan(0) // the seed holds a funded, unclaimed issue
  await page.getByRole('button', { name: 'Funded, unclaimed' }).click()
  await expect(rows).toHaveCount(funded.length)
  await expect(rows.first()).toContainText('Funded, unclaimed')

  // Signed out, a row opens the sign-in gate rather than the app.
  await rows.first().getByRole('link').click()
  await expect(theGate(page)).toBeVisible()
  await expect(page).toHaveURL(/\/$/)

  // Signed in, a row opens the issue's own page in the app.
  await signInThroughTheGate(page, 'contributor', 'landing-reader')
  await page.goto('/')
  await page.getByRole('button', { name: 'Funded, unclaimed' }).click()
  await rows.first().getByRole('link').click()
  await expect(page).toHaveURL(new RegExp(`/issues/${funded[0].id}$`))
  await expect(page.getByText(funded[0].title).first()).toBeVisible()
})

test('the landing page leads into the app, and the app back home', async ({ page, request }) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  await signInThroughTheGate(page, 'publisher', 'landing-pub', 'Landing Pub')
  await page.goto('/')
  await page.locator('.site-header').getByRole('link', { name: 'Open the app' }).click()
  await expect(page).toHaveURL(/\/overview$/)

  // The app's brand goes home to the landing page.
  await page.locator('nav.nav').getByRole('link', { name: 'Misthos home' }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(page.locator('.landing')).toBeVisible()

  // The landing page's way in leads to the app, which has pages of its own.
  await page.locator('.site-header').getByRole('link', { name: 'Open the app' }).click()
  await expect(page).toHaveURL(/\/overview$/)
  await page.getByRole('navigation').getByRole('link', { name: 'Overview' }).click()
  await expect(page).toHaveURL(/\/overview$/)
  await expect(page.getByRole('heading', { name: 'The treasury, running itself' })).toBeVisible()
})

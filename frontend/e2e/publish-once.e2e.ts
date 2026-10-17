import { expect, test } from '@playwright/test'
import { signInThroughTheGate } from './gate'
import { connectRepository } from './publish'

/**
 * #120: a double click on "Publish at this price" published the same GitHub issue
 * twice, and so did publishing it again later, which let one pull request be paid on
 * both. The button sends the form once, and the picker no longer offers an issue that
 * already has a listing (#199), so the second attempt cannot even be made.
 */
test('a GitHub issue is published once, and publishing it again links to it', async ({
  page,
  request,
}) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  const published: string[] = []
  page.on('request', (r) => {
    if (r.method() === 'POST' && new URL(r.url()).pathname === '/api/v1/issues') {
      published.push(r.url())
    }
  })

  await signInThroughTheGate(page, 'publisher', 'acme-widgets', 'Acme Widgets')
  await connectRepository(request, 'acme-widgets', 'acme/widgets')

  const fill = async () => {
    await page.getByRole('link', { name: 'Publish', exact: true }).click()
    await page.getByLabel('Repository').selectOption('acme/widgets')
    await page.getByLabel('Issue', { exact: true }).selectOption('77')
    await page.getByLabel('What the work is worth, in USDC').fill('250')
  }

  await fill()
  await page.getByRole('button', { name: 'Publish at this price' }).dblclick()
  await expect(page).toHaveURL(/\/issues\/ISS-\d+$/)
  expect(published).toHaveLength(1)

  // Back at the form the issue is still on GitHub, but it already has a listing, so
  // the picker says so and keeps it out of reach.
  await page.getByRole('link', { name: 'Publish', exact: true }).click()
  await page.getByLabel('Repository').selectOption('acme/widgets')
  const listed = page.getByLabel('Issue', { exact: true }).locator('option[value="77"]')
  await expect(listed).toContainText('already on Misthos')
  await expect(listed).toHaveAttribute('disabled', '')
  expect(published).toHaveLength(1)

  expect(errors).toEqual([])
})

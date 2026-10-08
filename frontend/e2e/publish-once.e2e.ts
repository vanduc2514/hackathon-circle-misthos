import { expect, test } from '@playwright/test'

/**
 * #120: a double click on "Publish and get a price" published the same GitHub issue
 * twice, and so did publishing it again later, which let one pull request be paid on
 * both. The button sends the form once, and a second publish of an open issue is
 * refused with a link to the listing it already has.
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

  await page.goto('/account')
  await page.getByRole('button', { name: 'Demo publisher wallet' }).click()
  await page.getByLabel('Organisation name').fill('Acme Widgets')
  await page.getByRole('button', { name: 'Continue as a publisher' }).click()
  await page.getByLabel('GitHub login').fill('acme-widgets')
  await page.getByRole('button', { name: 'Link this login' }).click()
  await expect(page.getByRole('link', { name: 'publisher · acme-widgets' })).toBeVisible()

  const fill = async (repo: string) => {
    await page.getByRole('link', { name: 'Publish', exact: true }).click()
    await page.getByLabel('Repository').fill(repo)
    await page.getByLabel('Issue number').fill('77')
    await page.getByLabel('Title').fill('Retry on 503')
  }

  await fill('acme/widgets')
  await page.getByRole('button', { name: 'Publish and get a price' }).dblclick()
  await expect(page).toHaveURL(/\/issues\/ISS-\d+$/)
  const first = page.url()
  expect(published).toHaveLength(1)

  await fill('Acme/Widgets')
  await page.getByRole('button', { name: 'Publish and get a price' }).click()
  const refused = page.locator('.error-box')
  await expect(refused).toContainText(/acme\/widgets#77 is already on Misthos as ISS-\d+/)
  await refused.getByRole('link', { name: /^Open ISS-\d+$/ }).click()
  await expect(page).toHaveURL(first)
  expect(published).toHaveLength(2)

  expect(errors).toEqual([])
})

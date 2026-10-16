import { expect, test } from '@playwright/test'

/**
 * Installing the App is a repository action, and it comes after linking.
 *
 * A connection is attributed to the publisher whose *linked* GitHub login installed it
 * (`connect_repositories`), so the install link is offered once GitHub is linked and
 * nowhere before it. Installing authorizes nothing about the person, which is why the
 * flow that treated it as a sign-in is gone; the link is a plain install page with no
 * state on it.
 */
test('installing the App is offered after linking, and is not a sign-in', async ({
  page,
  request,
}) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))

  await page.goto('/account')
  await page.getByRole('button', { name: 'Demo publisher wallet' }).click()
  await page.getByLabel('Your GitHub login').fill('acme-widgets')
  await page.getByRole('button', { name: 'Link this login' }).click()
  await page.getByLabel('Organisation name').fill('Acme Widgets')
  await page.getByRole('button', { name: 'Continue as a publisher' }).click()
  await expect(page.getByRole('link', { name: 'publisher · acme-widgets' })).toBeVisible()

  // Now the install link is offered, and it is the App's plain install page.
  const install = page.getByRole('link', { name: /Install the GitHub App/ })
  await expect(install).toBeVisible()
  await expect(install).toHaveAttribute(
    'href',
    'https://github.com/apps/misthos-dev/installations/new',
  )

  expect(errors).toEqual([])
})

/**
 * Once the App is installed, the button manages that installation instead of offering
 * an install the App is already on.
 *
 * GitHub's per-installation page is the only place a repository is added to or removed
 * from an installation, so it is where a publisher who has connected one needs to go.
 * The installation arrives the way it does in production: an `installation` delivery.
 */
test('a connected repository turns the install button into the App settings', async ({
  page,
  request,
}) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))

  await page.goto('/account')
  await page.getByRole('button', { name: 'Demo publisher wallet' }).click()
  await page.getByLabel('Your GitHub login').fill('acme-widgets')
  await page.getByRole('button', { name: 'Link this login' }).click()
  await page.getByLabel('Organisation name').fill('Acme Widgets')
  await page.getByRole('button', { name: 'Continue as a publisher' }).click()

  // Nothing installed: the button installs.
  await expect(page.getByRole('link', { name: /Install the GitHub App/ })).toBeVisible()

  // The App is installed on a repository, attributed to the linked login.
  const delivery = await request.post('/api/v1/webhooks/github', {
    headers: { 'X-GitHub-Event': 'installation', 'X-GitHub-Delivery': 'e2e-install-1' },
    data: {
      action: 'created',
      installation: { id: 4242 },
      sender: { login: 'acme-widgets' },
      repositories: [{ full_name: 'acme/widgets' }],
    },
  })
  expect(delivery.ok()).toBe(true)

  await page.reload()
  // The button is now the way to manage that installation, and the repository shows.
  const settings = page.getByRole('link', { name: 'Open misthos-dev settings' })
  await expect(settings).toBeVisible()
  await expect(settings).toHaveAttribute(
    'href',
    'https://github.com/settings/installations/4242',
  )
  await expect(page.getByRole('link', { name: /Install the GitHub App/ })).toHaveCount(0)

  expect(errors).toEqual([])
})

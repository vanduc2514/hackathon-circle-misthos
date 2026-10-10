import { expect, test, type Page } from '@playwright/test'

/**
 * The acceptance-criteria box belongs to the issue it is showing.
 *
 * The panel and the URL can change without React unmounting the panel, so
 * `useState(issue.acceptance_criteria...)` kept the *previous* issue's text: opening
 * A, then B, then going back to A left B's criteria in A's box, and "Approve these
 * criteria" then sent B's criteria to A. That is the human checkpoint the money is
 * released against, so it has to be the issue on screen.
 */

const BENIGN = [/\/api\/v1\/auth\/me$/, /fonts\.(googleapis|gstatic)\.com/]

function watchForErrors(page: Page, errors: string[]) {
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('response', (r) => {
    if (r.status() >= 400 && !BENIGN.some((b) => b.test(r.url()))) {
      errors.push(`${r.status()} ${r.url()}`)
    }
  })
}

async function onboardPublisher(page: Page, name: string, login: string) {
  await page.goto('/account')
  await page.getByRole('button', { name: 'Demo publisher wallet' }).click()
  await page.getByLabel('Organisation name').fill(name)
  await page.getByRole('button', { name: 'Continue as a publisher' }).click()
  await page.getByLabel('GitHub login').fill(login)
  await page.getByRole('button', { name: 'Link this login' }).click()
  await expect(page.getByRole('link', { name: `publisher · ${login}` })).toBeVisible()
}

test('the criteria box follows the issue, not the previous page', async ({ page, request }) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const errors: string[] = []
  watchForErrors(page, errors)

  // Two of this publisher's issues, each with its own drafted criteria.
  await onboardPublisher(page, 'Acme Ledger Co', 'acme-ledger')
  const published: string[] = []
  for (const title of ['Reject the unterminated locale string', 'Bound the retry backoff']) {
    await page.getByRole('link', { name: 'Publish', exact: true }).click()
    await page.getByLabel('Repository').fill('acme/ledger-core')
    await page.getByLabel('Title').fill(title)
    await page.getByLabel('Labels, comma separated').fill('bug')
    await page.getByRole('button', { name: 'Publish and get a price' }).click()
    await expect(page).toHaveURL(/\/issues\/ISS-\d+$/)
    published.push(page.url())
    // Replace the drafted criteria with a marker only this issue carries, so the two
    // boxes are provably different text rather than both showing the same draft.
    await page.getByRole('textbox').first().fill(`A test covers: ${title}`)
    await page.getByRole('button', { name: 'Approve these criteria' }).click()
    await expect(page.getByRole('button', { name: 'Criteria approved' })).toBeVisible()
  }

  const [firstUrl, secondUrl] = published
  const firstName = 'Reject the unterminated locale string'
  const secondName = 'Bound the retry backoff'

  // Every move below is a client-side navigation. `page.goto` would reload the app
  // and remount everything, which is what hides this bug: only React Router's in-app
  // moves keep the panel mounted while its `issue` prop changes.
  const box = page.getByRole('textbox').first()

  await page.getByRole('link', { name: 'Issues', exact: true }).click()
  await page.getByRole('link', { name: new RegExp(firstName) }).first().click()
  await expect(page).toHaveURL(firstUrl)
  await expect(box).toHaveValue(new RegExp(firstName))

  await page.getByRole('link', { name: 'Issues', exact: true }).click()
  await page.getByRole('link', { name: new RegExp(secondName) }).first().click()
  await expect(page).toHaveURL(secondUrl)
  await expect(box).toHaveValue(new RegExp(secondName))

  // The URL and the panel agree after every in-app move. The panel keeps its own
  // state while React Router changes the `issue` prop, so it is keyed by the issue id
  // (`key={issue.id}`); without that, a move between two issues on the same route
  // would leave the previous issue's criteria in the box, and "Approve these
  // criteria" would send them to the issue now on screen.
  await page.goBack()
  await expect(page).toHaveURL(/\/issues$/)
  await page.getByRole('link', { name: new RegExp(firstName) }).first().click()
  await expect(page).toHaveURL(firstUrl)
  await expect(page.getByRole('textbox').first()).toHaveValue(new RegExp(firstName))
  await expect(page.getByRole('textbox').first()).not.toHaveValue(new RegExp(secondName))

  expect(errors).toEqual([])
})

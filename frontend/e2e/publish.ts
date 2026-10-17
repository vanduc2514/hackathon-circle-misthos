import { expect, type APIRequestContext, type Page } from '@playwright/test'

/**
 * Publishing from the picker (#199): the form offers the repositories the App is
 * installed on and the issues in the chosen one, instead of asking for both to be
 * typed. The tests connect a repository the way production does, through the delivery
 * GitHub sends, and then fill the form.
 */

export async function connectRepository(
  request: APIRequestContext,
  login: string,
  repo: string,
  installation = 4242,
): Promise<void> {
  const delivery = await request.post('/api/v1/webhooks/github', {
    headers: {
      'X-GitHub-Event': 'installation',
      'X-GitHub-Delivery': `e2e-install-${login}-${repo.replace('/', '-')}`,
    },
    data: {
      action: 'created',
      installation: { id: installation },
      sender: { login },
      repositories: [{ full_name: repo }],
    },
  })
  expect(delivery.ok()).toBe(true)
}

/** Open the form and pick the repository whose issues should be offered. */
export async function chooseRepository(page: Page, repo: string): Promise<void> {
  await page.getByRole('link', { name: 'Publish', exact: true }).click()
  await page.getByLabel('Repository').selectOption(repo)
}

/** Publish work that is not on GitHub yet: the form opens the issue. */
export async function createIssue(
  page: Page,
  repo: string,
  title: string,
  labels?: string,
): Promise<void> {
  await chooseRepository(page, repo)
  await page.getByRole('button', { name: 'Create new issue' }).click()
  await page.getByLabel('Title').fill(title)
  if (labels) await page.getByLabel('Labels, comma separated').fill(labels)
}

/** Publish an issue that is already open on GitHub, by its number. */
export async function linkIssue(page: Page, repo: string, number: number): Promise<void> {
  await chooseRepository(page, repo)
  await page.getByLabel('Issue', { exact: true }).selectOption(String(number))
}

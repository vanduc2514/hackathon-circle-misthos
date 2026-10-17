import { expect, test } from '@playwright/test'
import { signInThroughTheGate } from './gate'
import { connectRepository, createIssue } from './publish'

/**
 * #142: the publisher names the price, and the number they name is committed.
 *
 * The engine used to propose a band and validate any choice inside it, so a publisher
 * whose instinct was outside the window could not transact at all. The engine's output is
 * advice now: it shows what comparable work settled for, and the two bounds on a price are
 * the platform's own.
 */
test('a publisher names the price, and that is what the escrow commits', async ({
  page,
  request,
}) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))

  await signInThroughTheGate(page, 'publisher', 'acme-ledger', 'Acme Ledger Co')

  // A price the engine would not have suggested: the form takes it, and says what the
  // split will be before anything is published.
  await connectRepository(request, 'acme-ledger', 'acme/ledger-core')
  await createIssue(
    page,
    'acme/ledger-core',
    'Retry idempotently when settlement returns 503',
  )
  await page.getByLabel('What the work is worth, in USDC').fill('40')
  await expect(page.getByText(/The contributor receives \$36\.00/)).toBeVisible()
  await page.getByRole('button', { name: 'Publish at this price' }).click()
  await expect(page).toHaveURL(/\/issues\/ISS-\d+$/)

  const issue = await page.evaluate(async () => {
    const id = location.pathname.split('/').pop()
    return (await (await fetch(`/api/v1/issues/${id}`)).json()) as {
      price_usdc: string
      proposal: { recommended: { usdc: string } }
    }
  })
  expect(issue.price_usdc).toBe('40.00')
  // And the engine disagrees loudly, which is exactly why it no longer decides.
  expect(Number(issue.proposal.recommended.usdc)).toBeGreaterThan(40)

  await page.getByRole('button', { name: 'Approve these criteria' }).click()
  await expect(page.getByRole('button', { name: 'Criteria approved' })).toBeVisible()

  // The publisher moves the price themselves while nothing is committed.
  await page.getByLabel('Change the price, in USDC').fill('250')
  await page.getByRole('button', { name: 'Set the price' }).click()
  await expect(page.getByRole('button', { name: /^Approve and commit \$250\.00$/ })).toBeVisible()

  // The form that moves USDC from another chain asks for that price, not the engine's
  // suggestion: 1842.75 would be 184x the issue, and prefilling it invites bridging it.
  const bridge = page
    .locator('section')
    .filter({ has: page.getByRole('heading', { name: 'Fund from another chain' }) })
  await expect(bridge.getByLabel('USDC')).toHaveValue('250.00')

  await page.getByRole('button', { name: /^Approve and commit \$250\.00$/ }).click()
  await expect(page.getByText('Funded and open')).toBeVisible()

  // What the escrow holds is the publisher's number, not the engine's.
  const escrow = await page.evaluate(async () => {
    const id = location.pathname.split('/').pop()
    return (await (await fetch(`/api/v1/issues/${id}/escrow`)).json()) as {
      amount: { usdc: string }
      escrow_ceiling: { usdc: string }
    }
  })
  expect(escrow.amount.usdc).toBe('250.00')
  expect(escrow.escrow_ceiling.usdc).toBe('250.00')

  expect(errors).toEqual([])
})

test('a price below the platform minimum is refused before anything is published', async ({
  page,
  request,
}) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)

  await signInThroughTheGate(page, 'publisher', 'acme-ledger', 'Acme Ledger Co')

  await connectRepository(request, 'acme-ledger', 'acme/ledger-core')
  await createIssue(
    page,
    'acme/ledger-core',
    'Retry idempotently when settlement returns 503',
  )
  await page.getByLabel('What the work is worth, in USDC').fill('1')
  await expect(page.getByText(/Any amount from \$5/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Publish at this price' })).toBeDisabled()
})

import { expect, test, type Page } from '@playwright/test'

/**
 * The app wears the landing page's look (#168): white pages, Georgia headings, the
 * landing's header and footer, and the network and money still named in the header.
 * A phone never scrolls the page sideways; a table scrolls inside its own panel.
 */

const PAGES = ['/overview', '/issues', '/issues/ISS-1004', '/account']

async function look(page: Page) {
  return page.evaluate(() => {
    const css = (el: Element | null) => (el ? getComputedStyle(el) : null)
    return {
      background: css(document.body)?.backgroundColor,
      headingFont: css(document.querySelector('.page-title, .detail-title'))?.fontFamily ?? '',
      sideways: document.documentElement.scrollWidth - window.innerWidth,
    }
  })
}

test('every app page is light, with the landing header and footer', async ({ page, request }) => {
  expect((await request.post('/api/v1/demo/reset')).ok()).toBe(true)
  const health = await (await request.get('/api/v1/health')).json()

  for (const path of PAGES) {
    await page.goto(path)
    const nav = page.locator('nav.nav')
    await expect(nav.getByRole('link', { name: 'Misthos home' })).toBeVisible()
    // The header still says which network and which money this deployment uses.
    await expect(nav).toContainText(health.network_label, { ignoreCase: true })
    await expect(nav).toContainText('simulated', { ignoreCase: true })
    await expect(page.locator('footer.footer')).toContainText(health.money_note)
    // The Account page loads on demand, so wait for its heading before reading the type.
    await expect(page.locator('.page-title, .detail-title').first()).toBeVisible()

    const { background, headingFont } = await look(page)
    expect(background, path).toBe('rgb(255, 255, 255)')
    expect(headingFont, path).toContain('Georgia')
  }
})

test('a phone never scrolls an app page sideways', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  for (const path of PAGES) {
    await page.goto(path)
    await expect(page.locator('nav.nav')).toBeVisible()
    const { sideways } = await look(page)
    expect(sideways, path).toBeLessThanOrEqual(0)
  }
})

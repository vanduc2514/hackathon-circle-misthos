import { defineConfig, devices } from '@playwright/test'

// Node's, declared rather than pulling in @types/node for one object.
declare const process: { env: Record<string, string | undefined> }

// A machine whose :8000 or :5173 already belongs to another project can move the
// browser tests aside instead of freeing the ports.
const apiPort = Number(process.env.E2E_API_PORT ?? 8000)
const webPort = Number(process.env.E2E_WEB_PORT ?? 5173)

/**
 * The browser tests: the whole loop driven through the web app, against the real
 * API in the simulation. Both servers are started here, so `mise run test:e2e` needs
 * nothing running beforehand; locally it reuses servers that already are.
 */
export default defineConfig({
  testDir: './e2e',
  testMatch: '*.e2e.ts',
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://localhost:${webPort}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    // A Chromium that is already installed, where downloading one is not possible.
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : {},
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: `uv run uvicorn misthos.main:app --port ${apiPort}`,
      cwd: '../backend',
      url: `http://127.0.0.1:${apiPort}/api/v1/health`,
      env: {
        MISTHOS_SIMULATED: 'true',
        MISTHOS_SESSION_SECRET: 'e2e-only-session-secret-not-for-deployment',
        // Each test starts from the seed. The data here is the tests' own to throw
        // away, even when a database is configured in backend/.env.
        MISTHOS_ALLOW_DEMO_RESET: 'true',
        // A fresh install has no GitHub OAuth App, which is what the browser tests
        // check the Account page against, whatever backend/.env says.
        MISTHOS_GITHUB_OAUTH_CLIENT_ID: '',
        MISTHOS_GITHUB_OAUTH_CLIENT_SECRET: '',
        // The browser tests are written against the rule reviewer, whose verdict is
        // decided by the file list in the same instant. A key in backend/.env would
        // send every verdict to the model instead, so the assertions would be racing
        // the network. Blanked here, the way tests/conftest.py blanks it for pytest.
        MISTHOS_ANTHROPIC_API_KEY: '',
        // And no App: with one configured the API reads real repositories and, when a
        // publish leaves the number empty, opens a real issue on GitHub. A browser test
        // must never do that, whatever backend/.env says.
        MISTHOS_GITHUB_APP_ID: '',
        MISTHOS_GITHUB_APP_PRIVATE_KEY: '',
        // The slug alone is not an App: it only lets the API hand out an install link,
        // which is what the install-button test needs. Nothing else changes.
        MISTHOS_GITHUB_APP_SLUG: 'misthos-dev',
      },
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: `npm run dev -- --host 0.0.0.0 --port ${webPort} --strictPort`,
      url: `http://127.0.0.1:${webPort}`,
      env: {
        // The web app proxies /api to the API this run started, wherever it landed.
        VITE_API_TARGET: `http://127.0.0.1:${apiPort}`,
      },
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
})

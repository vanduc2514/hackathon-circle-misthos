import { defineConfig, devices } from '@playwright/test'

// Node's, declared rather than pulling in @types/node for one object.
declare const process: { env: Record<string, string | undefined> }

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
    baseURL: 'http://localhost:5173',
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
      command: 'uv run uvicorn misthos.main:app --port 8000',
      cwd: '../backend',
      url: 'http://127.0.0.1:8000/api/v1/health',
      env: {
        MISTHOS_SIMULATED: 'true',
        MISTHOS_SESSION_SECRET: 'e2e-only-session-secret-not-for-deployment',
        // Each test starts from the seed. The data here is the tests' own to throw
        // away, even when a database is configured in backend/.env.
        MISTHOS_ALLOW_DEMO_RESET: 'true',
      },
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: 'npm run dev -- --port 5173 --strictPort',
      url: 'http://localhost:5173',
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
})

import { defineConfig, devices } from "@playwright/test";

// Standard browser-acceptance suite (M11A task §14). It never touches
// PostgreSQL: `AP_AGENT_API_BASE_URL` points at the lightweight mock
// backend in `tests/e2e/support/mock-backend.mjs`, which serves the exact
// controlled-fixture baseline shape documented in the task brief so the
// dashboard-rendering assertions are meaningful without a live database.
// The genuine FastAPI+PostgreSQL acceptance path (task §15) is a separate,
// explicitly-invoked suite: see `playwright.integration.config.ts`.
const PORT = 4311;
const MOCK_BACKEND_PORT = 4312;
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./tests/e2e",
  testIgnore: ["**/real-integration.spec.ts"],
  // Serial, not parallel: every spec file drives the same mock-backend
  // process's shared mutable `mode` (tests/e2e/support/mock-backend.mjs),
  // so two tests racing to set different modes would flake regardless of
  // file. The suite is small enough (~30 tests) that serial execution
  // costs seconds, not minutes.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [["html", { open: "never", outputFolder: "playwright-report" }], ["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // This sandbox pre-installs one pinned Chromium build that may lag
        // the exact revision `@playwright/test` expects; pointing directly
        // at it avoids an unnecessary/unavailable download. CI environments
        // without this pre-installed browser fall back to Playwright's own
        // managed browser via `npx playwright install` (task §18).
        launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
          ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
          : undefined,
      },
    },
  ],
  webServer: [
    {
      command: `node tests/e2e/support/mock-backend.mjs`,
      port: MOCK_BACKEND_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: `npm run build && npm run start -- --port ${PORT}`,
      url: BASE_URL,
      reuseExistingServer: !process.env.CI,
      timeout: 180_000,
      env: {
        AP_AGENT_API_BASE_URL: `http://127.0.0.1:${MOCK_BACKEND_PORT}`,
        AP_AGENT_FRONTEND_AUTH_MODE: "development_headers",
        AP_AGENT_DEV_TENANT_ID: "00000000-0000-0000-0000-000000000000",
        AP_AGENT_DEV_ACTOR_ID: "playwright-e2e",
        AP_AGENT_DEV_ACTOR_ROLE: "READ_ONLY_AUDITOR",
        AP_AGENT_BACKEND_TIMEOUT_MS: "1500",
      },
    },
  ],
});

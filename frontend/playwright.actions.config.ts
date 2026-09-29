import { defineConfig, devices } from "@playwright/test";

// M11C mocked review-action suite. Runs against the stateful mock backend
// (`tests/e2e/support/mock-backend-actions.mjs`) and four Next.js servers
// that differ only in their server-only command mode / actor role (see
// `tests/e2e/support/actions-stack.mjs`). No database is involved; the real
// FastAPI + PostgreSQL + Next.js path is `playwright.integration.config.ts`.
export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: ["actions-*.spec.ts"],
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 45_000,
  reporter: [["html", { open: "never", outputFolder: "playwright-report-actions" }], ["list"]],
  use: { trace: "on-first-retry", screenshot: "only-on-failure" },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : undefined,
      },
    },
  ],
  webServer: {
    command: "node tests/e2e/support/actions-stack.mjs",
    url: "http://127.0.0.1:4320",
    reuseExistingServer: !process.env.CI,
    timeout: 360_000,
    stdout: "ignore",
    stderr: "ignore",
  },
});

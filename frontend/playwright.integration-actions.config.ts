import { defineConfig, devices } from "@playwright/test";

// M11C real FastAPI + PostgreSQL + Next.js write-enabled acceptance. Like
// `playwright.integration.config.ts` it starts no servers itself:
// `scripts/run-real-actions.mjs` seeds the isolated tenant, starts the stack
// and passes its URLs/case ids in through `AP_AGENT_ACCEPTANCE_STACK`.
if (!process.env.AP_AGENT_ACCEPTANCE_STACK) {
  throw new Error("AP_AGENT_ACCEPTANCE_STACK is not set. Run this through `npm run test:e2e:real-actions`.");
}

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: ["real-actions.spec.ts"],
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  // The remote (Neon) database adds hundreds of ms per connection and the detail page re-reads several
  // times after each command, so refreshed state can take longer than Playwright's default 5s to appear.
  // Assertions are unchanged; only how long they may wait.
  timeout: 180_000,
  expect: { timeout: 45_000 },
  reporter: [["html", { open: "never", outputFolder: "playwright-report-real-actions" }], ["list"]],
  use: { trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : undefined,
      },
    },
  ],
});

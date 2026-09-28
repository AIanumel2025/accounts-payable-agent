import { defineConfig, devices } from "@playwright/test";

// Real FastAPI + PostgreSQL + Next.js acceptance path (M11A task §15).
// Unlike `playwright.config.ts`, this config starts no `webServer` itself
// -- `scripts/run-real-integration.mjs` already started the real FastAPI
// (against `AP_AGENT_TEST_POSTGRES_DSN`) and Next.js processes and passes
// their base URL in via `AP_AGENT_ACCEPTANCE_BASE_URL`.
const baseURL = process.env.AP_AGENT_ACCEPTANCE_BASE_URL;

if (!baseURL) {
  throw new Error(
    "AP_AGENT_ACCEPTANCE_BASE_URL is not set. Run this config through `npm run test:e2e:integration` " +
      "(scripts/run-real-integration.mjs), which starts the real FastAPI+PostgreSQL+Next.js stack first.",
  );
}

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: ["real-integration.spec.ts"],
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 60_000,
  reporter: [["html", { open: "never", outputFolder: "playwright-report-integration" }], ["list"]],
  use: {
    baseURL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
          ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
          : undefined,
      },
    },
  ],
});

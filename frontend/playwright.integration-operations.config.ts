import { defineConfig, devices } from "@playwright/test";

// M11D Core real FastAPI + worker + PostgreSQL + Next.js acceptance. Like the
// other integration configs it starts no servers itself:
// `scripts/run-real-operations.mjs` seeds the isolated tenant, starts the
// stack (FastAPI, ONE separate worker process, Next.js) and passes its URLs in
// through `AP_AGENT_ACCEPTANCE_STACK`.
if (!process.env.AP_AGENT_ACCEPTANCE_STACK) {
  throw new Error("AP_AGENT_ACCEPTANCE_STACK is not set. Run this through `npm run test:e2e:real-operations`.");
}

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: ["real-operations.spec.ts"],
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  // OCR runs in the worker (real OCR: tens of seconds) against a possibly remote database. Assertions are
  // unchanged; only how long they may wait.
  timeout: 420_000,
  expect: { timeout: 60_000 },
  reporter: [["html", { open: "never", outputFolder: "playwright-report-real-operations" }], ["list"]],
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

import { defineConfig, devices } from "@playwright/test";

// M11E hosted-mode acceptance: Clerk-style authentication (session cookies verified
// by the Next.js proxy and, independently, by FastAPI), database identity mapping,
// S3-compatible object storage, a separate worker and PostgreSQL. Like the other
// integration configs it starts no servers itself: `scripts/run-hosted-acceptance.mjs`
// starts the stack and passes its URLs and test-only session tokens in through
// `AP_AGENT_ACCEPTANCE_STACK`.
if (!process.env.AP_AGENT_ACCEPTANCE_STACK) {
  throw new Error("AP_AGENT_ACCEPTANCE_STACK is not set. Run this through `npm run test:e2e:hosted`.");
}

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: ["hosted-workflow.spec.ts"],
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 420_000,
  expect: { timeout: 60_000 },
  reporter: [["html", { open: "never", outputFolder: "playwright-report-hosted" }], ["list"]],
  use: { trace: "off", screenshot: "off" },
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

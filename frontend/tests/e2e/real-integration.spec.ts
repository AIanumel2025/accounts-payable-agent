import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

/**
 * Real FastAPI + PostgreSQL + Next.js acceptance path (M11A task §15).
 *
 * Runs only via `npm run test:e2e:integration`
 * (`scripts/run-real-integration.mjs`), which starts a genuine FastAPI
 * process against the isolated `ap_agent_m8_test` PostgreSQL database
 * (`AP_AGENT_TEST_POSTGRES_DSN`) and a genuine Next.js process pointed at
 * it, then hands this spec their real base URL. Nothing here is mocked.
 */

const EXPECTED_METRICS: Record<string, number> = {
  "Total invoices": 4,
  "Processing invoices": 0,
  "Completed automatically": 1,
  "Review required": 3,
  "Failed invoices": 0,
  "Open review cases": 3,
  "Unassigned review cases": 3,
};

const EXPECTED_REASONS: Record<string, number> = {
  "Inherited financial validation review": 3,
  "Supplier name missing": 2,
  "Invoice line total missing": 1,
};

test.describe("real FastAPI + PostgreSQL + Next.js acceptance", () => {
  test("dashboard matches the controlled-fixture baseline end to end", async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on("pageerror", (error) => consoleErrors.push(String(error)));
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });

    await page.goto("/dashboard");
    await expect(page.getByTestId("dashboard-body")).toBeVisible({ timeout: 30_000 });

    // Step 6: controlled-fixture metrics.
    for (const [label, value] of Object.entries(EXPECTED_METRICS)) {
      await expect(page.getByLabel(`${label}: ${value}`), `metric "${label}" should be ${value}`).toBeVisible();
    }

    // Step 7: review-reason analytics.
    const table = page.getByRole("table");
    await expect(table).toBeVisible();
    for (const [reason, count] of Object.entries(EXPECTED_REASONS)) {
      const row = table.locator("tr", { hasText: reason });
      await expect(row, `review reason "${reason}" should have count ${count}`).toContainText(String(count));
    }

    // Step 8: backend-health status.
    await expect(page.locator('[role="status"]').getByText("Backend connected")).toBeVisible();
    await expect(page.getByText("Read-only / validation-only")).toBeVisible();

    // Step 9: browser console errors.
    expect(consoleErrors, `unexpected console errors: ${consoleErrors.join("; ")}`).toEqual([]);

    // Step 10: accessibility scan.
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag22aa"]).analyze();
    const seriousOrCritical = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(seriousOrCritical, JSON.stringify(seriousOrCritical, null, 2)).toEqual([]);
  });

  test("no PostgreSQL mutation occurred: repeated reads return identical totals", async ({ page, request, baseURL }) => {
    // M11A is read-only (task §6): reloading the dashboard and re-reading
    // through the server-side proxy route must never change any total,
    // because no command endpoint is ever called and
    // AP_AGENT_ENABLE_REVIEW_COMMAND_WRITES stays unset/false throughout.
    await page.goto("/dashboard");
    await expect(page.getByTestId("dashboard-body")).toBeVisible({ timeout: 30_000 });
    const firstLoadText = await page.getByTestId("dashboard-body").innerText();

    const proxyResponse = await request.get(`${baseURL}/api/backend/api/v1/dashboard`);
    expect(proxyResponse.ok()).toBe(true);

    await page.reload();
    await expect(page.getByTestId("dashboard-body")).toBeVisible({ timeout: 30_000 });
    const secondLoadText = await page.getByTestId("dashboard-body").innerText();

    for (const [label, value] of Object.entries(EXPECTED_METRICS)) {
      await expect(page.getByLabel(`${label}: ${value}`)).toBeVisible();
    }

    expect(secondLoadText.replace(/Last refreshed.*/s, "")).toBe(firstLoadText.replace(/Last refreshed.*/s, ""));
  });
});

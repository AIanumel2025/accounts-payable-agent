import { expect, test } from "@playwright/test";
import { setMockBackendMode } from "./support/control";

test.beforeEach(async ({ request }) => {
  await setMockBackendMode(request, "normal");
});

test.describe("dashboard rendering", () => {
  test("shows a loading state before the dashboard data resolves", async ({ page }) => {
    const navigation = page.goto("/dashboard");
    await expect(page.getByTestId("loading-state")).toBeVisible();
    await navigation;
  });

  test("renders the controlled-fixture baseline metrics exactly", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByTestId("dashboard-body")).toBeVisible();

    await expect(page.getByLabel("Total invoices: 4")).toBeVisible();
    await expect(page.getByLabel("Processing invoices: 0")).toBeVisible();
    await expect(page.getByLabel("Completed automatically: 1")).toBeVisible();
    await expect(page.getByLabel("Review required: 3")).toBeVisible();
    await expect(page.getByLabel("Failed invoices: 0")).toBeVisible();
    await expect(page.getByLabel("Open review cases: 3")).toBeVisible();
    await expect(page.getByLabel("Unassigned review cases: 3")).toBeVisible();
  });

  test("renders the controlled-fixture review-reason analytics", async ({ page }) => {
    await page.goto("/dashboard");
    const table = page.getByRole("table");
    await expect(table).toBeVisible();

    const rows = table.locator("tbody tr");
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0)).toContainText("Inherited financial validation review");
    await expect(rows.nth(0)).toContainText("3");
    await expect(rows.nth(1)).toContainText("Supplier name missing");
    await expect(rows.nth(1)).toContainText("2");
    await expect(rows.nth(2)).toContainText("Invoice line total missing");
    await expect(rows.nth(2)).toContainText("1");
  });

  test("shows backend-health status as connected", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByText("Backend connected")).toBeVisible();
  });

  test("shows the validation-only safety indicator", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByText("Read-only / validation-only")).toBeVisible();
  });

  test("shows a last-refreshed timestamp", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByText(/Last refreshed/)).toBeVisible();
  });

  test.describe("responsive layout", () => {
    const sizes: Array<[string, { width: number; height: number }]> = [
      ["desktop", { width: 1440, height: 900 }],
      ["laptop", { width: 1280, height: 800 }],
      ["tablet", { width: 834, height: 1112 }],
      ["mobile", { width: 390, height: 844 }],
    ];

    for (const [name, viewport] of sizes) {
      test(`dashboard remains usable at ${name} width`, async ({ page }) => {
        await page.setViewportSize(viewport);
        await page.goto("/dashboard");
        await expect(page.getByTestId("dashboard-body")).toBeVisible();
        await expect(page.getByLabel("Total invoices: 4")).toBeVisible();

        const hasHorizontalScroll = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
        expect(hasHorizontalScroll).toBe(false);
      });
    }
  });
});

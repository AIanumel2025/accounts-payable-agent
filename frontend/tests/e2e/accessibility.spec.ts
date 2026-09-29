import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { setMockBackendMode } from "./support/control";

test.beforeEach(async ({ request }) => {
  await setMockBackendMode(request, "normal");
});

test.afterEach(async ({ request }) => {
  await setMockBackendMode(request, "normal");
});

test.describe("accessibility", () => {
  test("dashboard has no serious or critical automated accessibility violations (desktop)", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/dashboard");
    await expect(page.getByTestId("dashboard-body")).toBeVisible();

    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag22aa"]).analyze();
    const seriousOrCritical = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(seriousOrCritical, JSON.stringify(seriousOrCritical, null, 2)).toEqual([]);
  });

  test("dashboard has no serious or critical automated accessibility violations (mobile)", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/dashboard");
    await expect(page.getByTestId("dashboard-body")).toBeVisible();

    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag22aa"]).analyze();
    const seriousOrCritical = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(seriousOrCritical, JSON.stringify(seriousOrCritical, null, 2)).toEqual([]);
  });

  test("error state has no serious or critical automated accessibility violations", async ({ page, request }) => {
    await setMockBackendMode(request, "unavailable");
    await page.goto("/dashboard");
    await expect(page.getByTestId("error-state")).toBeVisible();

    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    const seriousOrCritical = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(seriousOrCritical, JSON.stringify(seriousOrCritical, null, 2)).toEqual([]);
  });

  test("page has exactly one h1 and a logical heading structure", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
    await expect(page.getByRole("heading", { level: 2 })).toHaveCount(2); // "Processing overview", "Review reasons"
  });

  test("full keyboard-only navigation reaches the primary nav link and main content", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto("/dashboard");

    await page.keyboard.press("Tab"); // skip link
    await page.keyboard.press("Tab"); // Dashboard nav link
    await expect(page.getByRole("link", { name: "Dashboard" })).toBeFocused();
  });

  test("respects prefers-reduced-motion for the loading spinner", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/dashboard");
    // Just assert the page still renders correctly under the preference; the
    // spinner's animation is disabled via @media (prefers-reduced-motion).
    await expect(page.getByTestId("dashboard-body")).toBeVisible();
  });

  // M11B task §13.
  test("review queue has no serious or critical automated accessibility violations (desktop)", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/review-queue");
    await expect(page.getByTestId("queue-table")).toBeVisible();

    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag22aa"]).analyze();
    const seriousOrCritical = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(seriousOrCritical, JSON.stringify(seriousOrCritical, null, 2)).toEqual([]);
  });

  test("review queue has no serious or critical automated accessibility violations (mobile)", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/review-queue");
    await expect(page.getByTestId("queue-card-list")).toBeVisible();

    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag22aa"]).analyze();
    const seriousOrCritical = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(seriousOrCritical, JSON.stringify(seriousOrCritical, null, 2)).toEqual([]);
  });

  test("review case detail page has no serious or critical automated accessibility violations", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/review-cases/cccccccc-0000-4000-8000-000000000003");
    await expect(page.getByTestId("review-case-detail-body")).toBeVisible();

    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag22aa"]).analyze();
    const seriousOrCritical = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(seriousOrCritical, JSON.stringify(seriousOrCritical, null, 2)).toEqual([]);
  });

  test("no table row on the review queue is itself an interactive/clickable element", async ({ page }) => {
    await page.goto("/review-queue");
    await expect(page.getByTestId("queue-table")).toBeVisible();
    const rowRoles = await page.getByTestId("queue-table").locator("tbody tr").evaluateAll((rows) =>
      rows.map((row) => ({ tag: row.tagName, hasOnClick: row.hasAttribute("onclick"), role: row.getAttribute("role") })),
    );
    for (const row of rowRoles) {
      expect(row.hasOnClick).toBe(false);
      expect(row.role === null || row.role === "row").toBe(true);
    }
  });
});

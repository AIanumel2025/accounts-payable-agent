import { expect, test } from "@playwright/test";
import { setMockBackendMode } from "./support/control";

test.beforeEach(async ({ request }) => {
  await setMockBackendMode(request, "normal");
});

test.describe("review queue", () => {
  test("shows a loading state before the queue data resolves", async ({ page }) => {
    const navigation = page.goto("/review-queue");
    await expect(page.getByTestId("loading-state")).toBeVisible();
    await navigation;
  });

  test("renders the three controlled-fixture review cases", async ({ page }) => {
    await page.goto("/review-queue");
    await expect(page.getByTestId("queue-table")).toBeVisible();

    await expect(page.getByRole("link", { name: "Open review case for 36258" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Open review case for Template1_Instance90.jpg" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Open review case for 308044" })).toBeVisible();
  });

  test("never renders a fully-clickable table row -- only an explicit detail link per row (task §13)", async ({ page }) => {
    await page.goto("/review-queue");
    const rows = page.getByTestId("queue-table").locator("tbody tr");
    await expect(rows).toHaveCount(3);
    // Exactly one link per row identity cell, not the row itself.
    for (let i = 0; i < 3; i += 1) {
      await expect(rows.nth(i).getByRole("link")).toHaveCount(1);
    }
  });

  test("status filter navigates and updates the URL, preserved on reload", async ({ page }) => {
    await page.goto("/review-queue");
    await page.getByLabel("Status").selectOption("OPEN");
    await expect(page).toHaveURL(/status=OPEN/);
    await expect(page.getByText(/matching cases/)).toBeVisible();

    await page.reload();
    await expect(page).toHaveURL(/status=OPEN/);
    await expect(page.getByLabel("Status")).toHaveValue("OPEN");
  });

  test("only offers the four backend-supported status values", async ({ page }) => {
    await page.goto("/review-queue");
    const options = await page.getByLabel("Status").locator("option").allTextContents();
    expect(options).toEqual(["All statuses", "Open", "In review", "Resolved", "Rejected"]);
  });

  test("clear-all-filters resets the URL and re-shows every case", async ({ page }) => {
    await page.goto("/review-queue?priority=CRITICAL");
    await expect(page.getByRole("link", { name: "Open review case for 36258" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Open review case for Template1_Instance90.jpg" })).toHaveCount(0);

    await page.getByRole("button", { name: "Clear all filters" }).click();
    await expect(page).toHaveURL(/\/review-queue$/);
    await expect(page.getByRole("link", { name: "Open review case for Template1_Instance90.jpg" })).toBeVisible();
  });

  test("a filter with no matches shows the no-filtered-results state, distinct from a truly empty queue", async ({ page }) => {
    await page.goto("/review-queue?priority=LOW");
    await expect(page.getByText("No matching review cases")).toBeVisible();
    await expect(page.getByText("Try clearing a filter")).toBeVisible();
  });

  test("a malformed URL parameter is dropped silently rather than erroring the page", async ({ page }) => {
    await page.goto("/review-queue?status=NOT_A_REAL_STATUS&batch_id=not-a-uuid");
    await expect(page.getByTestId("queue-table")).toBeVisible();
    await expect(page.getByRole("link", { name: "Open review case for 36258" })).toBeVisible();
  });

  test("pagination controls are present and correctly reflect a single page of results", async ({ page }) => {
    await page.goto("/review-queue");
    await expect(page.getByText("Page 1 of 1")).toBeVisible();
    await expect(page.getByRole("button", { name: "Previous" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  test("dashboard metric links navigate into the queue with the exact backend-supported filter", async ({ page }) => {
    await page.goto("/dashboard");
    await page.getByRole("link", { name: /Open review cases: 3/ }).click();
    await expect(page).toHaveURL(/\/review-queue\?status=OPEN/);
  });

  test.describe("responsive layout", () => {
    // The table/card breakpoint is 768px (QueueResultsTable.module.css); at
    // or above it the <table> is the visible presentation, below it the
    // card list is -- the other one is `display: none`, not merely
    // visually hidden, so each viewport must assert on the container that
    // is actually visible at that width.
    const sizes: Array<[string, { width: number; height: number }, "table" | "card"]> = [
      ["desktop", { width: 1440, height: 900 }, "table"],
      ["tablet", { width: 834, height: 1112 }, "table"],
      ["mobile", { width: 390, height: 844 }, "card"],
    ];

    for (const [name, viewport, presentation] of sizes) {
      test(`review queue remains usable at ${name} width, review reasons never hidden`, async ({ page }) => {
        await page.setViewportSize(viewport);
        await page.goto("/review-queue");

        const container =
          presentation === "table" ? page.getByTestId("queue-table") : page.getByTestId("queue-card-list");
        await expect(container).toBeVisible();
        await expect(container.getByText("Supplier name missing").first()).toBeVisible();

        const hasHorizontalScroll = await page.evaluate(
          () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
        );
        expect(hasHorizontalScroll).toBe(false);
      });
    }
  });
});

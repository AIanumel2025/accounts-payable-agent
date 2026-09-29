import { expect, test } from "@playwright/test";
import { setMockBackendMode } from "./support/control";

test.beforeEach(async ({ request }) => {
  await setMockBackendMode(request, "normal");
});

test.describe("navigation", () => {
  test("root route redirects to /dashboard", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/dashboard$/);
  });

  test("desktop sidebar shows Dashboard active and Review queue as an active link (M11B task §3)", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto("/dashboard");

    const dashboardLink = page.getByRole("link", { name: "Dashboard" });
    await expect(dashboardLink).toHaveAttribute("aria-current", "page");

    const reviewQueueLink = page.getByRole("link", { name: "Review queue" });
    await expect(reviewQueueLink).toBeVisible();
    await expect(reviewQueueLink).toHaveAttribute("href", "/review-queue");
    await expect(reviewQueueLink).not.toHaveAttribute("aria-current", "page");
  });

  test("Review queue nav link navigates to the queue", async ({ page }) => {
    await page.goto("/dashboard");
    await page.getByRole("link", { name: "Review queue" }).click();
    await expect(page).toHaveURL(/\/review-queue$/);
    const reviewQueueLink = page.getByRole("link", { name: "Review queue" });
    await expect(reviewQueueLink).toHaveAttribute("aria-current", "page");
  });

  test("no Payments navigation item exists anywhere on the page", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByText("Payments", { exact: false })).toHaveCount(0);
  });

  test("skip-to-content link is the first focusable element and moves focus to main", async ({ page }) => {
    await page.goto("/dashboard");
    await page.keyboard.press("Tab");
    const skipLink = page.locator(".skip-link");
    await expect(skipLink).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.locator("#main-content")).toBeFocused();
  });

  test("mobile viewport shows the compact nav toggle instead of the desktop sidebar", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/dashboard");

    await expect(page.getByRole("button", { name: /open navigation/i })).toBeVisible();
    await expect(page.locator("#mobile-nav-drawer")).toHaveCount(0);

    await page.getByRole("button", { name: /open navigation/i }).click();
    await expect(page.locator("#mobile-nav-drawer")).toBeVisible();
  });
});

import { expect, test } from "@playwright/test";
import { setMockBackendMode } from "./support/control";

test.afterEach(async ({ request }) => {
  await setMockBackendMode(request, "normal");
});

test.describe("backend failure handling", () => {
  test("backend unavailable renders a helpful error, not a stack trace", async ({ page, request }) => {
    await setMockBackendMode(request, "unavailable");
    await page.goto("/dashboard");

    const errorState = page.getByTestId("error-state");
    await expect(errorState).toBeVisible();
    await expect(errorState).toContainText("Backend unavailable");

    const bodyText = await page.locator("body").innerText();
    expect(bodyText).not.toMatch(/postgres|password|Traceback|dsn|neon\.tech/i);
  });

  test("backend unavailable also reflects in the header's backend indicator", async ({ page, request }) => {
    await setMockBackendMode(request, "unavailable");
    await page.goto("/dashboard");
    // Scoped to the header's live-region indicator: the main-content error
    // state also happens to say "Backend unavailable" in its title.
    await expect(page.locator('[role="status"]').getByText("Backend unavailable")).toBeVisible();
  });

  test("malformed upstream response is handled without crashing the page", async ({ page, request }) => {
    await setMockBackendMode(request, "malformed");
    await page.goto("/dashboard");

    const errorState = page.getByTestId("error-state");
    await expect(errorState).toBeVisible();
    await expect(errorState).toContainText("Unexpected response");
  });

  test("request timeout is handled distinctly and offers retry", async ({ page, request }) => {
    await setMockBackendMode(request, "timeout");
    await page.goto("/dashboard");

    const errorState = page.getByTestId("error-state");
    await expect(errorState).toBeVisible({ timeout: 10_000 });
    await expect(errorState).toContainText("Request timed out");
    await expect(errorState.getByRole("button", { name: "Retry" })).toBeVisible();
  });

  test("retry recovers the dashboard once the backend is healthy again", async ({ page, request }) => {
    await setMockBackendMode(request, "unavailable");
    await page.goto("/dashboard");
    await expect(page.getByTestId("error-state")).toBeVisible();

    await setMockBackendMode(request, "normal");
    await page.getByRole("button", { name: "Retry" }).click();

    await expect(page.getByTestId("dashboard-body")).toBeVisible();
    await expect(page.getByLabel("Total invoices: 4")).toBeVisible();
  });

  test("backend health recheck button detects recovery and shows a stale note while down", async ({ page, request }) => {
    await setMockBackendMode(request, "normal");
    await page.goto("/dashboard");
    await expect(page.getByText("Backend connected")).toBeVisible();

    await setMockBackendMode(request, "unavailable");
    await page.getByTestId("backend-health-retry").click();
    await expect(page.getByText("Backend unavailable")).toBeVisible();
    await expect(page.getByTestId("backend-stale-note")).toBeVisible();

    await setMockBackendMode(request, "normal");
    await page.getByTestId("backend-health-retry").click();
    await expect(page.getByText("Backend connected")).toBeVisible();
  });
});

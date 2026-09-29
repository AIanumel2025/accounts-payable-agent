import { expect, test } from "@playwright/test";
import { setMockBackendMode } from "./support/control";

const TEMPLATE1_ID = "aaaaaaaa-0000-4000-8000-000000000001";
const CASE_08181_ID = "bbbbbbbb-0000-4000-8000-000000000002";
const AARON_ID = "cccccccc-0000-4000-8000-000000000003";
const UNKNOWN_ID = "00000000-0000-4000-8000-000000000099";

test.beforeEach(async ({ request }) => {
  await setMockBackendMode(request, "normal");
});

test.describe("review case detail", () => {
  test("navigating from the queue reaches the detail page with a working breadcrumb back", async ({ page }) => {
    await page.goto("/review-queue?priority=HIGH");
    await page.getByRole("link", { name: "Open review case for Template1_Instance90.jpg" }).click();
    await expect(page).toHaveURL(new RegExp(`/review-cases/${TEMPLATE1_ID}`));

    const breadcrumbLink = page.getByRole("navigation", { name: "Breadcrumb" }).getByRole("link", { name: "Review queue" });
    await expect(breadcrumbLink).toBeVisible();
    await breadcrumbLink.click();

    // Preserves the filter that was active when the user left the queue (task §2).
    await expect(page).toHaveURL(/\/review-queue\?priority=HIGH/);
  });

  test("shows a loading state before the detail data resolves", async ({ page }) => {
    const navigation = page.goto(`/review-cases/${TEMPLATE1_ID}`);
    await expect(page.getByTestId("loading-state")).toBeVisible();
    await navigation;
  });

  test("renders every section (A-H) for a populated case", async ({ page }) => {
    await page.goto(`/review-cases/${AARON_ID}`);
    await expect(page.getByTestId("review-case-detail-body")).toBeVisible();

    await expect(page.getByRole("heading", { name: "Identity and status" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Normalized fields" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Evidence references" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Financial validation" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Supplier and purchase-order matching" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Line matches" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Timeline" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Previous review decisions" })).toBeVisible();
  });

  test("never converts a missing value to zero -- shows the explicit missing-value marker instead", async ({ page }) => {
    // Template1's SUPPLIER_NAME/INVOICE_NUMBER fields are genuinely missing in the fixture.
    await page.goto(`/review-cases/${TEMPLATE1_ID}`);
    await expect(page.getByTestId("review-case-detail-body")).toBeVisible();
    const fieldsTable = page.locator('[aria-label="Normalized invoice fields, scrollable"]');
    await expect(fieldsTable.getByText("—").first()).toBeVisible();
    await expect(fieldsTable).not.toContainText("0.00");
  });

  test("a case with zero line matches (08181) shows the line-matches empty state, not a broken table", async ({ page }) => {
    await page.goto(`/review-cases/${CASE_08181_ID}`);
    await expect(page.getByTestId("review-case-detail-body")).toBeVisible();
    await expect(page.getByText("No line matches recorded")).toBeVisible();
  });

  test("a fresh case with no reviewer decisions shows the decisions empty state, never a fabricated one", async ({ page }) => {
    await page.goto(`/review-cases/${TEMPLATE1_ID}`);
    await expect(page.getByTestId("review-case-detail-body")).toBeVisible();
    await expect(page.getByText("No review decisions recorded")).toBeVisible();
  });

  test("an unknown review-case id renders a safe 404, not a leaked internal error", async ({ page }) => {
    await page.goto(`/review-cases/${UNKNOWN_ID}`);
    const errorState = page.getByTestId("error-state");
    await expect(errorState).toBeVisible();
    await expect(errorState).toContainText("Not found");

    const bodyText = await page.locator("body").innerText();
    expect(bodyText).not.toMatch(/postgres|password|Traceback|dsn/i);
  });

  test("a malformed (non-UUID) review-case id also renders the same safe 404, never a 500", async ({ page }) => {
    const response = await page.goto("/review-cases/not-a-uuid");
    expect(response?.status()).toBeLessThan(500);
    await expect(page.getByTestId("error-state")).toContainText("Not found");
  });

  test("original_document_uri stays unavailable, and no local filesystem path is ever shown", async ({ page }) => {
    await page.goto(`/review-cases/${TEMPLATE1_ID}`);
    await expect(page.getByTestId("review-case-detail-body")).toBeVisible();
    await expect(page.getByText(/Not available \(document viewing/)).toBeVisible();

    const bodyText = await page.locator("body").innerText();
    expect(bodyText).not.toMatch(/\/home\/|\/tmp\/|^[A-Za-z]:\\/m);
  });

  test("no command or mutation request is ever issued while browsing the detail page", async ({ page }) => {
    const commandRequests: string[] = [];
    page.on("request", (request) => {
      if (/\/commands\b/.test(request.url())) commandRequests.push(request.url());
      if (request.method() !== "GET" && request.url().includes("/api/")) commandRequests.push(request.url());
    });

    await page.goto(`/review-cases/${AARON_ID}`);
    await expect(page.getByTestId("review-case-detail-body")).toBeVisible();

    expect(commandRequests).toEqual([]);
  });

  test.describe("responsive layout", () => {
    const sizes: Array<[string, { width: number; height: number }]> = [
      ["desktop", { width: 1440, height: 900 }],
      ["mobile", { width: 390, height: 844 }],
    ];

    for (const [name, viewport] of sizes) {
      test(`detail page remains usable at ${name} width`, async ({ page }) => {
        await page.setViewportSize(viewport);
        await page.goto(`/review-cases/${AARON_ID}`);
        await expect(page.getByTestId("review-case-detail-body")).toBeVisible();

        const hasHorizontalScroll = await page.evaluate(
          () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
        );
        expect(hasHorizontalScroll).toBe(false);
      });
    }
  });
});

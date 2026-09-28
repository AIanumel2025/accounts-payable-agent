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

/**
 * The three M11B named acceptance fixtures' `review_case_id`s
 * (`scripts/manage_m11a_acceptance_tenant.py`'s seed output, passed
 * through by `scripts/run-real-integration.mjs` as this env var -- see
 * that script's own comment). Never hardcoded: a fresh tenant and fresh
 * ids are generated every run.
 */
function namedFixtureReviewCaseIds(): Record<string, string> {
  const raw = process.env.AP_AGENT_ACCEPTANCE_REVIEW_CASE_IDS;
  if (!raw) {
    throw new Error(
      "AP_AGENT_ACCEPTANCE_REVIEW_CASE_IDS is not set. Run this spec through `npm run test:e2e:integration` " +
        "(scripts/run-real-integration.mjs), which seeds the named fixtures and passes their ids through.",
    );
  }
  return JSON.parse(raw);
}

/**
 * Exact per-fixture detail counts (M11B task §9), cross-checked against
 * real Phase 1-8 golden baselines (`tests/support/review_fixtures.py`'s
 * own module comment documents exactly how each number was derived --
 * not arbitrary placeholders). The aggregate row is these three summed:
 * 25 fields, 65 evidence references, 32 financial checks, 6 line matches.
 */
const NAMED_FIXTURE_DETAIL_COUNTS: Record<
  string,
  {
    sourceName: string;
    /** The queue's primaryLabel(item) for this fixture -- invoice_number when set, else source_name. */
    queueLabel: string;
    fields: number;
    evidence: number;
    checks: number;
    lineMatches: number;
  }
> = {
  // invoice_number is null for Template1, so its queue label is its source_name.
  template1: { sourceName: "Template1_Instance90.jpg", queueLabel: "Template1_Instance90.jpg", fields: 9, evidence: 18, checks: 12, lineMatches: 5 },
  "08181_warped": {
    sourceName: "08181_warped_document_perspective_shadow.jpg",
    queueLabel: "308044",
    fields: 6,
    evidence: 25,
    checks: 12,
    lineMatches: 0,
  },
  aaron_bergman: {
    sourceName: "invoice_Aaron Bergman_36258.pdf",
    queueLabel: "36258",
    fields: 10,
    evidence: 22,
    checks: 8,
    lineMatches: 1,
  },
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

  test("review queue matches the controlled-fixture baseline end to end (M11B task §18)", async ({ page }) => {
    await page.goto("/review-queue");
    await expect(page.getByTestId("queue-table")).toBeVisible({ timeout: 30_000 });

    for (const fixture of Object.values(NAMED_FIXTURE_DETAIL_COUNTS)) {
      await expect(page.getByRole("link", { name: `Open review case for ${fixture.queueLabel}` })).toBeVisible();
    }
    await expect(page.getByText("3 matching cases")).toBeVisible();

    // Filters against a real, live query.
    await page.getByLabel("Status").selectOption("OPEN");
    await expect(page).toHaveURL(/status=OPEN/);
    await expect(page.getByTestId("queue-table")).toBeVisible();

    await page.getByRole("button", { name: "Clear all filters" }).click();
    await expect(page).toHaveURL(/\/review-queue$/);

    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag22aa"]).analyze();
    const seriousOrCritical = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(seriousOrCritical, JSON.stringify(seriousOrCritical, null, 2)).toEqual([]);
  });

  for (const [fixtureKey, expected] of Object.entries(NAMED_FIXTURE_DETAIL_COUNTS)) {
    test(`${fixtureKey} detail page matches its exact seeded counts (M11B task §9/§18)`, async ({ page }) => {
      const reviewCaseId = namedFixtureReviewCaseIds()[fixtureKey];
      expect(reviewCaseId, `no seeded review_case_id for fixture "${fixtureKey}"`).toBeTruthy();

      await page.goto(`/review-cases/${reviewCaseId}`);
      await expect(page.getByTestId("review-case-detail-body")).toBeVisible({ timeout: 30_000 });

      // Section A: identity.
      await expect(page.getByText(expected.sourceName)).toBeVisible();

      // Section B: normalized fields -- exact row count.
      const fieldsRows = page.locator('[aria-label="Normalized invoice fields, scrollable"] tbody tr');
      await expect(fieldsRows).toHaveCount(expected.fields);

      // Section C: evidence references -- exact total.
      const evidenceTotal = page
        .getByText("Total evidence references")
        .locator("xpath=following-sibling::dd[1]");
      await expect(evidenceTotal).toHaveText(String(expected.evidence));

      // Section D: financial validation -- exact check count.
      const checksRows = page.locator('[aria-label="Financial validation checks, scrollable"] tbody tr');
      await expect(checksRows).toHaveCount(expected.checks);

      // Section F: line matches -- exact count, or the empty state for 0.
      if (expected.lineMatches === 0) {
        await expect(page.getByText("No line matches recorded")).toBeVisible();
      } else {
        const lineMatchRows = page.locator(
          '[aria-label="Invoice line to purchase order line matches, scrollable"] tbody tr',
        );
        await expect(lineMatchRows).toHaveCount(expected.lineMatches);
      }

      // Section G: real audit-event timeline (seeded via a genuine
      // `append_audit_event` call, not a placeholder).
      await expect(page.getByRole("heading", { name: "Timeline" })).toBeVisible();
      await expect(page.getByText("Memory record created")).toBeVisible();

      // Section H: a fresh case has no reviewer decisions yet.
      await expect(page.getByText("No review decisions recorded")).toBeVisible();

      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag22aa"]).analyze();
      const seriousOrCritical = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
      expect(seriousOrCritical, JSON.stringify(seriousOrCritical, null, 2)).toEqual([]);
    });
  }

  test("aggregate detail counts across the three named fixtures match the task baseline exactly", async () => {
    const totals = Object.values(NAMED_FIXTURE_DETAIL_COUNTS).reduce(
      (acc, fixture) => ({
        fields: acc.fields + fixture.fields,
        evidence: acc.evidence + fixture.evidence,
        checks: acc.checks + fixture.checks,
        lineMatches: acc.lineMatches + fixture.lineMatches,
      }),
      { fields: 0, evidence: 0, checks: 0, lineMatches: 0 },
    );
    expect(totals).toEqual({ fields: 25, evidence: 65, checks: 32, lineMatches: 6 });
  });

  test("no PostgreSQL mutation occurred while browsing the queue and every detail page", async ({ page, request, baseURL }) => {
    await page.goto("/review-queue");
    await expect(page.getByTestId("queue-table")).toBeVisible({ timeout: 30_000 });
    const beforeQueueText = await page.getByTestId("queue-table").innerText();

    for (const reviewCaseId of Object.values(namedFixtureReviewCaseIds())) {
      await page.goto(`/review-cases/${reviewCaseId}`);
      await expect(page.getByTestId("review-case-detail-body")).toBeVisible({ timeout: 30_000 });
    }

    const proxyResponse = await request.get(`${baseURL}/api/backend/api/v1/review-cases`);
    expect(proxyResponse.ok()).toBe(true);

    await page.goto("/review-queue");
    await expect(page.getByTestId("queue-table")).toBeVisible({ timeout: 30_000 });
    const afterQueueText = await page.getByTestId("queue-table").innerText();

    expect(afterQueueText.replace(/\d+ (?:month|day|hour|minute|second)s? ago/g, "<relative>")).toBe(
      beforeQueueText.replace(/\d+ (?:month|day|hour|minute|second)s? ago/g, "<relative>"),
    );
  });
});

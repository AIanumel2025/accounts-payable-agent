import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { claimViaUi, fillCorrection, result, summary, watchConsole, workspace } from "./support/actions";

/**
 * M11C real FastAPI + PostgreSQL + Next.js acceptance (write-enabled).
 * Started by `scripts/run-real-actions.mjs`, which seeds one review case per
 * scenario in an isolated tenant and afterwards inspects PostgreSQL directly
 * (`scripts/manage_m11c_tenant.py verify`) -- the browser assertions below
 * and those database assertions together are the acceptance result.
 */

interface Stack {
  urls: { a: string; b: string; auditor: string; otherTenant: string; validation: string };
  api: { write: string; validation: string };
  tenantId: string;
  otherTenantId: string;
  reviewers: { a: string; b: string };
  cases: Record<string, string>;
  otherCases: Record<string, string>;
}

const stack = JSON.parse(process.env.AP_AGENT_ACCEPTANCE_STACK ?? "null") as Stack | null;
if (stack === null) throw new Error("AP_AGENT_ACCEPTANCE_STACK is required.");
const S: Stack = stack;
const shotsDir = process.env.AP_AGENT_SCREENSHOT_DIR;
if (shotsDir) mkdirSync(shotsDir, { recursive: true });

/** Screenshots (only when AP_AGENT_SCREENSHOT_DIR is set): the action workspace element by default, or the viewport for modal dialogs. */
async function shot(page: Page, name: string, target: "workspace" | "viewport" | "decisions" = "workspace") {
  if (!shotsDir) return;
  const file = path.join(shotsDir, `${name}.png`);
  if (target === "workspace") await workspace(page).screenshot({ path: file });
  else if (target === "decisions") await page.locator("section[aria-labelledby='section-decisions']").screenshot({ path: file });
  else await page.screenshot({ path: file });
}

async function open(page: Page, base: string, key: string) {
  await page.goto(`${base}/review-cases/${S.cases[key]}`);
  await expect(workspace(page)).toBeVisible();
}

/** The CSRF token the server embedded in the page (the same one the UI's own request carries). */
async function pageToken(page: Page): Promise<string> {
  const token = /csrfToken\\?":\\?"([0-9]+\.[A-Za-z0-9_-]+)/.exec(await page.content())?.[1];
  expect(token, "page carries a CSRF token").toBeTruthy();
  return token!;
}

function commandBody(action: string, overrides: Record<string, unknown> = {}) {
  const disposition = ({ ACCEPT: "APPROVED", CORRECT: "CORRECTED", REJECT: "REJECTED" } as Record<string, string>)[action] ?? null;
  const id = crypto.randomUUID();
  return {
    command_id: id, idempotency_key: `real-acceptance-${id}`, action, disposition, observed_review_revision: 1, observed_workflow_revision: 2,
    reason_codes: action === "CLAIM" || action === "RELEASE" ? [] : ["REVIEW_COMPLETED"], notes: action === "REJECT" ? "n" : null, corrections: [], ...overrides,
  };
}

async function forge(request: APIRequestContext, base: string, caseKey: string, token: string, body: unknown) {
  return request.post(`${base}/api/v1/review-cases/${S.cases[caseKey]}/commands`, { headers: { origin: base, "x-csrf-token": token }, data: body });
}

/** Direct FastAPI call with development headers -- test-only, to prove the *backend* refuses what the UI never sends. */
async function direct(request: APIRequestContext, api: string, tenantId: string, caseId: string, actor: string, role: string, body: unknown) {
  return request.post(`${api}/api/v1/review-cases/${caseId}/commands`, {
    headers: { "X-Tenant-ID": tenantId, "X-Actor-ID": actor, "X-Actor-Role": role, "X-Authenticated-At": new Date(Date.now() - 5_000).toISOString() },
    data: { ...(body as object), requested_at: new Date().toISOString() },
  });
}

const alertBox = (page: Page) => page.getByRole("alert").filter({ hasText: "was not completed" });

test.describe("A. claim and release", () => {
  test("claim, inspect, release -- and the queue and dashboard follow the database", async ({ page }) => {
    const problems = watchConsole(page);
    await open(page, S.urls.a, "claim_release");
    await expect(summary(page)).toContainText("Unassigned");
    await shot(page, "01-unclaimed-case");

    await claimViaUi(page);
    await expect(summary(page)).toContainText("Workflow revision2");
    await shot(page, "02-claimed-case");
    await page.reload();
    await expect(summary(page)).toContainText("Claimed by you"); // re-read from PostgreSQL

    await page.goto(`${S.urls.a}/review-queue`);
    await expect(page.locator(`tr:has(a[href*="${S.cases.claim_release}"])`)).toContainText(S.reviewers.a);

    await open(page, S.urls.a, "claim_release");
    await page.getByRole("button", { name: "Release" }).click();
    await expect(result(page)).toHaveAttribute("data-result", "executed");
    await expect(summary(page)).toContainText("Unassigned");
    await expect(page.getByRole("button", { name: "Claim this case" })).toBeVisible();
    expect(problems).toEqual([]);
  });
});

test.describe("B. approve and resume", () => {
  test("claim, approve, resume: handoff at memory persistence", async ({ page }) => {
    await open(page, S.urls.a, "approve_resume");
    await claimViaUi(page);

    await page.getByRole("button", { name: "Approve", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Approve this invoice?" });
    await dialog.getByRole("checkbox", { name: "Reviewer verified the invoice" }).check();
    await shot(page, "05-approve-confirmation", "viewport");
    await dialog.getByRole("button", { name: "Approve invoice" }).click();

    await expect(result(page)).toHaveAttribute("data-result", "executed");
    await expect(summary(page)).toContainText("Resolved");
    await expect(page.locator("section[aria-labelledby='section-decisions']")).toContainText("Approved");
    await expect(page.locator("section[aria-labelledby='section-decisions']")).toContainText(S.reviewers.a);
    await shot(page, "09-resolved-decision-history", "decisions");
    await expect(page.getByTestId("resume-panel")).toBeVisible();
    await shot(page, "10-resume-eligible");

    await page.getByRole("button", { name: "Request workflow resume" }).click();
    await expect(result(page)).toContainText("Downstream execution has not run yet.");
    await expect(page.getByTestId("resume-restart-stage")).toHaveText("Memory persistence");
    await expect(page.getByTestId("resume-derived-version")).toContainText("human-review-derived-");
    await shot(page, "11-resume-handoff-result");

    await page.reload();
    await expect(page.getByTestId("resume-unavailable")).toContainText("already created");
  });
});

test.describe("C. evidence-backed correction and resume", () => {
  test("a line correction (LINE_QUANTITY) restarts at financial validation", async ({ page }) => {
    await open(page, S.urls.a, "correct_line");
    await claimViaUi(page);
    await page.getByRole("button", { name: "Correct", exact: true }).click();
    await page.getByTestId("correction-row-0").getByLabel("Field to correct").selectOption("L:1:LINE_QUANTITY");
    await expect(page.getByTestId("correction-current-0")).toHaveText("5");
    await fillCorrection(page, { target: "L:1:LINE_QUANTITY", value: "6" });
    await shot(page, "03-correction-editor");
    await page.getByRole("button", { name: "Review and submit correction" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Submit corrections" }).click();

    await expect(result(page)).toHaveAttribute("data-result", "executed");
    await expect(summary(page)).toContainText("Resolved");
    await expect(page.locator("section[aria-labelledby='section-decisions']")).toContainText("Corrected");
    await page.getByRole("button", { name: "Request workflow resume" }).click();
    await expect(page.getByTestId("resume-restart-stage")).toHaveText("Financial validation");
  });

  test("a header reference correction (PURCHASE_ORDER_NUMBER) restarts at reference matching", async ({ page }) => {
    await open(page, S.urls.a, "correct_header");
    await claimViaUi(page);
    await page.getByRole("button", { name: "Correct", exact: true }).click();
    await fillCorrection(page, { target: "H:PURCHASE_ORDER_NUMBER", value: "99A" });
    await shot(page, "04-evidence-selection");
    await page.getByRole("button", { name: "Review and submit correction" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Submit corrections" }).click();
    await expect(summary(page)).toContainText("Resolved");
    // The original normalized value is still shown as recorded (derived corrections never overwrite it).
    await expect(page.locator("section[aria-labelledby='section-fields']")).toContainText("99");
    await page.getByRole("button", { name: "Request workflow resume" }).click();
    await expect(page.getByTestId("resume-restart-stage")).toHaveText("Reference matching");
  });
});

test.describe("D. reject", () => {
  test("reject with reasons and notes; resume is unavailable", async ({ page }) => {
    await open(page, S.urls.a, "reject");
    await claimViaUi(page);
    await page.getByRole("button", { name: "Reject", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Reject this invoice?" });
    await dialog.getByRole("checkbox", { name: "Duplicate invoice" }).check();
    await dialog.getByLabel("Notes (required)").fill("Already paid under another invoice number");
    await shot(page, "06-reject-confirmation", "viewport");
    await dialog.getByRole("button", { name: "Reject invoice" }).click();
    await expect(result(page)).toHaveAttribute("data-result", "executed");
    await expect(summary(page)).toContainText("Rejected");
    await expect(page.locator("section[aria-labelledby='section-decisions']")).toContainText("Already paid");
    await expect(page.getByTestId("resume-panel")).toHaveCount(0);
  });
});

test.describe("E. genuinely concurrent claim", () => {
  test("two reviewers click Claim at the same moment: exactly one wins", async ({ browser }) => {
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const pageA = await contextA.newPage();
    const pageB = await contextB.newPage();
    await Promise.all([open(pageA, S.urls.a, "concurrent"), open(pageB, S.urls.b, "concurrent")]);
    await expect(pageA.getByRole("button", { name: "Claim this case" })).toBeEnabled();
    await expect(pageB.getByRole("button", { name: "Claim this case" })).toBeEnabled();

    await Promise.all([
      pageA.getByRole("button", { name: "Claim this case" }).click(),
      pageB.getByRole("button", { name: "Claim this case" }).click(),
    ]);
    await Promise.all([expect(result(pageA)).not.toHaveAttribute("data-result", ""), expect(result(pageB)).not.toHaveAttribute("data-result", "")]);
    await expect.poll(async () => (await result(pageA).getAttribute("data-result")) !== null && (await result(pageB).getAttribute("data-result")) !== null).toBe(true);

    const outcomes = [await result(pageA).getAttribute("data-result"), await result(pageB).getAttribute("data-result")].sort();
    expect(outcomes).toEqual(["error", "executed"]);
    const loser = (await result(pageA).getAttribute("data-result")) === "error" ? pageA : pageB;
    await expect(alertBox(loser)).toContainText(/Already claimed|no longer open|changed/i);
    await expect(alertBox(loser)).not.toContainText(/STALE_|CASE_NOT_OPEN|postgres/i);

    await Promise.all([pageA.reload(), pageB.reload()]);
    const texts = [await summary(pageA).innerText(), await summary(pageB).innerText()];
    expect(texts.filter((text) => text.includes("Claimed by you"))).toHaveLength(1);
    expect(texts.filter((text) => text.includes("Claimed by another reviewer"))).toHaveLength(1);
    await contextA.close();
    await contextB.close();
  });
});

test.describe("F. idempotency", () => {
  test("a lost response is retried with the same identity; a reused key with new content conflicts", async ({ page }) => {
    await open(page, S.urls.a, "idempotency");
    const seen: Record<string, unknown>[] = [];
    let first = true;
    await page.route("**/api/v1/review-cases/*/commands", async (route) => {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      seen.push(body);
      if (first) {
        first = false;
        await route.fetch(); // executed against PostgreSQL...
        await route.abort("connectionreset"); // ...but the reply is lost
        return;
      }
      if (seen.length >= 3) body.idempotency_key = seen[0]!.idempotency_key; // force key reuse for different content
      await route.continue({ postData: JSON.stringify(body) });
    });

    await page.getByRole("button", { name: "Claim this case" }).click();
    await expect(alertBox(page)).toContainText("Network error");
    await page.getByRole("button", { name: "Claim this case" }).click();
    await expect(result(page)).toHaveAttribute("data-result", "idempotent");
    expect(seen[1]!.command_id).toBe(seen[0]!.command_id);
    expect(seen[1]!.idempotency_key).toBe(seen[0]!.idempotency_key);

    await page.getByRole("button", { name: "Release" }).click();
    await expect(alertBox(page)).toContainText("Conflicting duplicate request");
  });
});

test.describe("G. stale revision", () => {
  test("acting on a page that is out of date returns a conflict and mutates nothing", async ({ browser }) => {
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();
    const pageA = await contextA.newPage();
    const pageB = await contextB.newPage();
    await open(pageA, S.urls.a, "stale"); // loaded before B acts
    await open(pageB, S.urls.b, "stale");
    await claimViaUi(pageB);

    await pageA.getByRole("button", { name: "Claim this case" }).click();
    await expect(alertBox(pageA)).toContainText(/changed|no longer open|Already claimed/i);
    await shot(pageA, "08-stale-conflict");
    await expect(pageA.getByTestId("refresh-required")).toBeVisible();
    await pageA.getByRole("button", { name: "Refresh case data" }).click();
    await expect(summary(pageA)).toContainText("Claimed by another reviewer");
    await contextA.close();
    await contextB.close();
  });
});

test.describe("H. authorization and tenancy", () => {
  test("auditor, other reviewer and other tenant cannot act; nothing changes", async ({ page, browser, request }) => {
    // Auditor: no controls, and a forged request is refused by the backend.
    await open(page, S.urls.auditor, "authz");
    await expect(page.getByTestId("mode-banner")).toHaveAttribute("data-mode", "no-permission");
    await expect(workspace(page).getByRole("button")).toHaveCount(0);
    const auditorToken = await pageToken(page);
    const forgedByAuditor = await forge(request, S.urls.auditor, "authz", auditorToken, commandBody("CLAIM", { observed_workflow_revision: 1 }));
    expect(forgedByAuditor.status()).toBe(403);
    expect((await forgedByAuditor.json()).errors).toContain("ACTION_NOT_PERMITTED");

    // Reviewer A claims (the state the others then try to break).
    const contextA = await browser.newContext();
    const pageA = await contextA.newPage();
    await open(pageA, S.urls.a, "authz");
    await claimViaUi(pageA);

    // Reviewer B sees no controls and cannot release/approve/reject A's case even with a forged request.
    const contextB = await browser.newContext();
    const pageB = await contextB.newPage();
    await open(pageB, S.urls.b, "authz");
    await expect(summary(pageB)).toContainText("Claimed by another reviewer");
    await expect(workspace(pageB).getByRole("button")).toHaveCount(0);
    const tokenB = await pageToken(pageB);
    for (const action of ["RELEASE", "ACCEPT", "REJECT"]) {
      const forged = await forge(request, S.urls.b, "authz", tokenB, commandBody(action));
      expect(forged.status(), action).toBe(409);
      expect((await forged.json()).errors).toContain("CASE_ASSIGNED_TO_DIFFERENT_REVIEWER");
    }

    // Another tenant: the case does not exist for them (UI and backend).
    const contextC = await browser.newContext();
    const pageC = await contextC.newPage();
    await pageC.goto(`${S.urls.otherTenant}/review-cases/${S.cases.authz}`);
    await expect(pageC.getByText(/could not be found|Not found/i).first()).toBeVisible();
    await expect(pageC.getByTestId("review-action-workspace")).toHaveCount(0);
    const crossTenant = await direct(request, S.api.write, S.otherTenantId, S.cases.authz!, "other-tenant-reviewer", "AP_REVIEWER", commandBody("RELEASE"));
    expect(crossTenant.status()).toBe(404);

    await Promise.all([contextA.close(), contextB.close(), contextC.close()]);
  });

  test("a reviewer of another tenant only ever sees their own tenant's case", async ({ browser }) => {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(`${S.urls.otherTenant}/review-cases/${S.otherCases.cross_tenant}`);
    await expect(workspace(page)).toBeVisible();
    await expect(page.getByRole("button", { name: "Claim this case" })).toBeEnabled(); // seen, not clicked
    await page.goto(`${S.urls.a}/review-cases/${S.otherCases.cross_tenant}`);
    await expect(page.getByText(/could not be found|Not found/i).first()).toBeVisible();
    await context.close();
  });
});

test.describe("I. invalid corrections", () => {
  test("every invalid correction is rejected without partial mutation", async ({ page, request }) => {
    await open(page, S.urls.a, "invalid_correction");
    await claimViaUi(page);
    const token = await pageToken(page);

    const correction = (overrides: Record<string, unknown> = {}) => ({
      field_name: "PURCHASE_ORDER_NUMBER", line_number: null, previous_value: "99", corrected_value: "99A",
      reason: "PO verified against the order", evidence_reference_ids: ["ev-po-1"], ...overrides,
    });
    const attempt = (overrides: Record<string, unknown>) =>
      forge(request, S.urls.a, "invalid_correction", token, commandBody("CORRECT", { reason_codes: ["EVIDENCE_VERIFIED"], corrections: [correction(overrides)] }));

    const cases: [string, Record<string, unknown>, string][] = [
      ["missing reason", { reason: "   " }, "CORRECTION_REASON_REQUIRED"],
      ["missing evidence", { evidence_reference_ids: [] }, "CORRECTION_EVIDENCE_REQUIRED"],
      ["unknown evidence id", { evidence_reference_ids: ["ev-not-real"] }, "UNKNOWN_EVIDENCE_REFERENCE"],
      ["unknown line number", { field_name: "LINE_QUANTITY", line_number: 99, previous_value: "5", corrected_value: "6" }, "UNKNOWN_INVOICE_LINE_NUMBER"],
      ["stale previous value", { previous_value: "not-the-stored-value" }, "PREVIOUS_VALUE_MISMATCH"],
    ];
    for (const [label, overrides, code] of cases) {
      const response = await attempt(overrides);
      expect(response.status(), label).toBe(422);
      expect((await response.json()).errors, label).toContain(code);
    }
    const unsupportedField = await attempt({ field_name: "ACCOUNT_NUMBER" });
    expect(unsupportedField.status()).toBe(422);
  });
});

test.describe("J. payment, bank and ERP prohibition", () => {
  test("payment-style payloads are rejected before any transactional execution", async ({ page, request }) => {
    await open(page, S.urls.a, "payment");
    const token = await pageToken(page);
    const caseId = S.cases.payment!;
    for (const action of ["EXECUTE_PAYMENT", "RELEASE_PAYMENT", "BANK_TRANSFER", "POST_TO_ERP"]) {
      const viaNext = await forge(request, S.urls.a, "payment", token, commandBody("CLAIM", { action, observed_workflow_revision: 1 }));
      expect(viaNext.status(), `next ${action}`).toBe(422);
      const viaApi = await direct(request, S.api.write, S.tenantId, caseId, S.reviewers.a, "AP_REVIEWER", commandBody("CLAIM", { action, observed_workflow_revision: 1 }));
      expect(viaApi.status(), `api ${action}`).toBe(422);
    }
    for (const extra of [{ payment_amount: "10.00" }, { bank_account: "GB00" }, { erp_posting: true }]) {
      const viaApi = await direct(request, S.api.write, S.tenantId, caseId, S.reviewers.a, "AP_REVIEWER", { ...commandBody("CLAIM", { observed_workflow_revision: 1 }), ...extra });
      expect(viaApi.status(), JSON.stringify(extra)).toBe(422);
    }
    const openapi = await (await request.get(`${S.api.write}/openapi.json`)).json();
    expect(JSON.stringify(openapi)).not.toMatch(/EXECUTE_PAYMENT|RELEASE_PAYMENT/);
  });
});

test.describe("validation-only against a real validation-only backend", () => {
  test("VALIDATED: no database changes", async ({ page }) => {
    await open(page, S.urls.validation, "validation_only");
    await expect(page.getByTestId("mode-banner")).toHaveAttribute("data-mode", "validation_only");
    await page.getByRole("button", { name: "Claim this case" }).click();
    await expect(result(page)).toHaveAttribute("data-result", "validated");
    await expect(result(page)).toContainText("Validated — no database changes were made.");
    await shot(page, "07-validation-only-result");
    await page.reload();
    await expect(summary(page)).toContainText("Unassigned");
  });
});

test.describe("responsive and performance", () => {
  test("mobile action workspace", async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await context.newPage();
    await open(page, S.urls.a, "spare");
    await expect(page.getByRole("button", { name: "Claim this case" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
    await workspace(page).scrollIntoViewIfNeeded();
    await shot(page, "12-mobile-action-workspace");
    await context.close();
  });

  test("timing: page load, command submission, post-command refresh (real stack)", async ({ page }) => {
    const problems = watchConsole(page);
    const commandRequests: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().includes("/commands")) commandRequests.push(request.url());
    });

    const loadStarted = Date.now();
    await open(page, S.urls.a, "spare");
    const pageLoadMs = Date.now() - loadStarted;

    const submitStarted = Date.now();
    await page.getByRole("button", { name: "Claim this case" }).click();
    await expect(result(page)).toHaveAttribute("data-result", "executed");
    const submitMs = Date.now() - submitStarted;
    await expect(summary(page)).toContainText("Claimed by you");
    const refreshMs = Date.now() - submitStarted - submitMs;

    await page.getByRole("button", { name: "Release" }).click();
    await expect(summary(page)).toContainText("Unassigned");

    console.log(`METRICS ${JSON.stringify({ pageLoadMs, commandSubmitToResultMs: submitMs, resultToRefreshedStateMs: refreshMs, commandRequests: commandRequests.length })}`);
    expect(commandRequests).toHaveLength(2); // exactly one POST per user action: no duplicate requests
    expect(problems).toEqual([]);
  });
});

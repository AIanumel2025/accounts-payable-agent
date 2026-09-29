import { expect, test } from "@playwright/test";
import {
  APPROVE_CASE, CLAIM_CASE, CORRECT_CASE, OTHER_CASE, RACE_CASE, REJECT_CASE, RESUME_CASE, URLS,
  caseId, claimViaUi, control, fillCorrection, openCase, resetMock, result, stats, summary, watchConsole, workspace,
} from "./support/actions";

test.beforeEach(async ({ request }) => resetMock(request));

test.describe("control visibility", () => {
  test("a read-only auditor sees no active controls even in commit mode", async ({ page }) => {
    await openCase(page, URLS.auditor, CLAIM_CASE);
    await expect(page.getByTestId("mode-banner")).toHaveAttribute("data-mode", "no-permission");
    await expect(workspace(page).getByRole("button")).toHaveCount(0);
  });

  test("a reviewer sees Claim on an open case, and nothing on someone else's", async ({ page }) => {
    await openCase(page, URLS.commit, CLAIM_CASE);
    await expect(page.getByRole("button", { name: "Claim this case" })).toBeEnabled();
    await expect(page.getByRole("button", { name: /^(Approve|Reject|Correct|Release)$/ })).toHaveCount(0);

    await openCase(page, URLS.commit, OTHER_CASE);
    await expect(page.getByTestId("no-actions")).toContainText("claimed by another reviewer");
    await expect(workspace(page).getByRole("button")).toHaveCount(0);
  });

  test("deferred and payment actions have no controls anywhere", async ({ page }) => {
    await openCase(page, URLS.commit, CLAIM_CASE);
    await claimViaUi(page);
    const buttons = (await workspace(page).getByRole("button").allTextContents()).join(" | ");
    expect(buttons).not.toMatch(/supplier|purchase order|request information|escalate|pay|transfer|erp/i);
  });

  test("disabled mode: read-only banner, no controls, and no POST ever reaches the backend", async ({ page, request }) => {
    await openCase(page, URLS.disabled, CLAIM_CASE);
    await expect(page.getByTestId("mode-banner")).toHaveAttribute("data-mode", "disabled");
    await expect(workspace(page).getByRole("button")).toHaveCount(0);

    // Even a hand-crafted request is refused by the server boundary.
    const forged = await request.post(`${URLS.disabled}/api/v1/review-cases/${caseId(CLAIM_CASE)}/commands`, {
      headers: { origin: URLS.disabled, "x-csrf-token": "anything" },
      data: { command_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", idempotency_key: "forged-key-0001", action: "CLAIM", disposition: null, observed_review_revision: 1, observed_workflow_revision: 1, reason_codes: [], notes: null, corrections: [] },
    });
    expect(forged.status()).toBe(403);
    expect((await forged.json()).errors).toEqual(["REVIEW_COMMANDS_DISABLED"]);
    expect((await stats(request)).commandPosts).toBe(0);
  });
});

test.describe("validation-only mode", () => {
  test.beforeEach(async ({ request }) => control(request, { health_mode: "VALIDATION_ONLY" }));

  test("a claim returns VALIDATED and never pretends the case changed", async ({ page, request }) => {
    await openCase(page, URLS.validation, CLAIM_CASE);
    await expect(page.getByTestId("mode-banner")).toHaveAttribute("data-mode", "validation_only");
    await page.getByRole("button", { name: "Claim this case" }).click();

    await expect(result(page)).toHaveAttribute("data-result", "validated");
    await expect(result(page)).toContainText("Validated — no database changes were made.");
    await expect(result(page)).not.toContainText("Executed —");
    await expect(summary(page)).toContainText("Unassigned");
    await expect(summary(page)).toContainText("Open");

    await page.reload();
    await expect(summary(page)).toContainText("Unassigned");
    await expect(page.getByRole("button", { name: "Claim this case" })).toBeEnabled();
    expect((await stats(request)).commandPosts).toBe(1);
  });

  test("workflow resume is unavailable", async ({ page }) => {
    await openCase(page, URLS.validation, CLAIM_CASE);
    await expect(page.getByTestId("resume-panel")).toHaveCount(0);
  });
});

test.describe("mode mismatch fails closed", () => {
  test("frontend commit + backend VALIDATION_ONLY shows a mismatch and offers no controls", async ({ page, request }) => {
    await control(request, { health_mode: "VALIDATION_ONLY" });
    await openCase(page, URLS.commit, CLAIM_CASE);
    await expect(page.getByTestId("mode-banner")).toHaveAttribute("data-mode", "mismatch");
    await expect(workspace(page).getByRole("button")).toHaveCount(0);
  });

  test("frontend validation_only + backend COMMIT shows a mismatch and offers no controls", async ({ page }) => {
    await openCase(page, URLS.validation, CLAIM_CASE); // mock reports COMMIT by default
    await expect(page.getByTestId("mode-banner")).toHaveAttribute("data-mode", "mismatch");
    await expect(workspace(page).getByRole("button")).toHaveCount(0);
  });
});

test.describe("claim and release (scenario A)", () => {
  test("claim, then release, refreshing status and the queue/dashboard from the backend", async ({ page }) => {
    const problems = watchConsole(page);
    const unassigned = () => page.getByRole("listitem").filter({ hasText: "Unassigned review cases" });

    await page.goto(`${URLS.commit}/dashboard`);
    await expect(unassigned()).toContainText("6");

    await openCase(page, URLS.commit, CLAIM_CASE);
    await expect(summary(page)).toContainText("Workflow revision1");
    await claimViaUi(page);
    await expect(summary(page)).toContainText("In review");
    await expect(summary(page)).toContainText("Workflow revision2");
    await expect(page.getByRole("button", { name: "Release" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Claim this case" })).toHaveCount(0);
    await expect(page.getByText("Human-review case claimed by reviewer.").first()).toBeVisible(); // timeline refreshed

    await page.goto(`${URLS.commit}/dashboard`);
    await expect(unassigned()).toContainText("5"); // claim: unassigned count decreases

    await page.goBack();
    await page.getByRole("button", { name: "Release" }).click();
    await expect(result(page)).toHaveAttribute("data-result", "executed");
    await expect(summary(page)).toContainText("Unassigned");
    await expect(summary(page)).toContainText("Open");
    await expect(page.getByRole("button", { name: "Claim this case" })).toBeVisible();
    await expect(page.getByText("Human-review case released to the queue.").first()).toBeVisible();

    await page.goto(`${URLS.commit}/dashboard`);
    await expect(unassigned()).toContainText("6"); // release: unassigned count increases
    expect(problems).toEqual([]);
  });

  test("the queue and dashboard reflect a committed claim", async ({ page }) => {
    await page.goto(`${URLS.commit}/review-queue`);
    await expect(page.getByRole("link", { name: /INV-CLAIM-001/ }).first()).toBeVisible();
    await expect(page.getByText("Unassigned").first()).toBeVisible();

    await openCase(page, URLS.commit, CLAIM_CASE);
    await claimViaUi(page);

    await page.goto(`${URLS.commit}/review-queue`);
    const row = page.getByRole("row", { name: /INV-CLAIM-001/ });
    await expect(row).toContainText("reviewer-1");
    await expect(row).not.toContainText("Unassigned");
  });
});

test.describe("approve (scenario B)", () => {
  test("approve needs confirmation, records an append-only decision, and only then exposes resume", async ({ page }) => {
    await openCase(page, URLS.commit, APPROVE_CASE);
    await claimViaUi(page);
    await expect(page.getByTestId("resume-panel")).toHaveCount(0);

    await page.getByRole("button", { name: "Approve", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Approve this invoice?" });
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("append-only");
    await expect(dialog.getByRole("button", { name: "Approve invoice" })).toBeDisabled();
    await dialog.getByRole("checkbox", { name: "Totals verified" }).check();
    await dialog.getByRole("button", { name: "Approve invoice" }).click();

    await expect(result(page)).toHaveAttribute("data-result", "executed");
    await expect(result(page)).toContainText("Executed — the database was updated and the action was recorded.");
    await expect(summary(page)).toContainText("Resolved");
    const decisions = page.getByRole("region", { name: "Previous review decisions" }).or(page.locator("section[aria-labelledby='section-decisions']"));
    await expect(decisions).toContainText("Approved");
    await expect(decisions).toContainText("Totals Verified");
    await expect(page.getByTestId("resume-panel")).toContainText("controlled handoff only");
  });

  test("approved resume creates a memory-persistence handoff and nothing more (scenario B)", async ({ page }) => {
    await openCase(page, URLS.commit, RESUME_CASE);
    await claimViaUi(page);
    await page.getByRole("button", { name: "Approve", exact: true }).click();
    await page.getByRole("dialog").getByRole("checkbox", { name: "Reviewer verified the invoice" }).check();
    await page.getByRole("dialog").getByRole("button", { name: "Approve invoice" }).click();
    await expect(summary(page)).toContainText("Resolved");

    await page.getByRole("button", { name: "Request workflow resume" }).click();
    await expect(result(page)).toContainText("A controlled workflow-resume handoff was created. Downstream execution has not run yet.");
    await expect(page.getByTestId("resume-restart-stage")).toHaveText("Memory persistence");
    await expect(page.getByTestId("resume-derived-version")).toContainText("human-review-derived-");
    await expect(page.getByTestId("resume-unavailable")).toContainText("already created");
    await expect(page.getByRole("button", { name: "Request workflow resume" })).toHaveCount(0);
    await expect(summary(page)).toContainText("Workflow revision4");
  });
});

test.describe("evidence-backed correction (scenario C)", () => {
  test("a correction with a reason and real evidence resolves the case and resumes at the right stage", async ({ page }) => {
    await openCase(page, URLS.commit, CORRECT_CASE);
    await claimViaUi(page);
    await page.getByRole("button", { name: "Correct", exact: true }).click();

    const row = page.getByTestId("correction-row-0");
    await row.getByLabel("Field to correct").selectOption("H:TOTAL_AMOUNT");
    await expect(page.getByTestId("correction-current-0")).toHaveText("100.10");
    await expect(page.getByText("original normalized invoice record is immutable")).toBeVisible();
    await row.getByLabel("Corrected value").fill("100.100");
    await row.getByLabel("Reason for this correction").fill("The total on the scan reads 100.100");
    await row.getByRole("checkbox", { name: /ev-total-1/ }).check();
    await page.getByRole("group", { name: "Reason codes" }).getByRole("checkbox", { name: "Verified against evidence" }).check();
    await page.getByRole("button", { name: "Review and submit correction" }).click();

    const dialog = page.getByRole("dialog", { name: "Submit these corrections?" });
    await expect(dialog.getByTestId("correction-summary")).toContainText("100.10 → 100.100");
    await dialog.getByRole("button", { name: "Submit corrections" }).click();

    await expect(result(page)).toHaveAttribute("data-result", "executed");
    await expect(summary(page)).toContainText("Resolved");
    await expect(page.locator("section[aria-labelledby='section-decisions']")).toContainText("Corrected");
    await expect(page.locator("section[aria-labelledby='section-fields']")).toContainText("100.10"); // original memory unchanged

    await page.getByRole("button", { name: "Request workflow resume" }).click();
    await expect(page.getByTestId("resume-restart-stage")).toHaveText("Financial validation");
  });

  test("a header reference correction restarts at reference matching", async ({ page }) => {
    await openCase(page, URLS.commit, CORRECT_CASE);
    await claimViaUi(page);
    await page.getByRole("button", { name: "Correct", exact: true }).click();
    await fillCorrection(page, { target: "H:PURCHASE_ORDER_NUMBER", value: "99A" });
    await page.getByRole("button", { name: "Review and submit correction" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Submit corrections" }).click();
    await expect(summary(page)).toContainText("Resolved");
    await page.getByRole("button", { name: "Request workflow resume" }).click();
    await expect(page.getByTestId("resume-restart-stage")).toHaveText("Reference matching");
  });

  test("missing reason and missing evidence are caught inline, focus moves to the summary, and input is kept", async ({ page, request }) => {
    await openCase(page, URLS.commit, CORRECT_CASE);
    await claimViaUi(page);
    await page.getByRole("button", { name: "Correct", exact: true }).click();
    const row = page.getByTestId("correction-row-0");
    await row.getByLabel("Field to correct").selectOption("H:PURCHASE_ORDER_NUMBER");
    await row.getByLabel("Corrected value").fill("99A");
    await page.getByRole("group", { name: "Reason codes" }).getByRole("checkbox", { name: "Verified against evidence" }).check();
    await page.getByRole("button", { name: "Review and submit correction" }).click();

    const summaryBox = page.getByTestId("correction-error-summary");
    await expect(summaryBox).toBeFocused();
    await expect(summaryBox).toContainText("meaningful reason");
    await expect(summaryBox).toContainText("evidence reference");
    await expect(row.getByLabel("Corrected value")).toHaveValue("99A");
    expect((await stats(request)).commandPosts).toBe(1); // only the claim; nothing was sent
  });

  test("evidence choices come only from the case", async ({ page }) => {
    await openCase(page, URLS.commit, CORRECT_CASE);
    await claimViaUi(page);
    await page.getByRole("button", { name: "Correct", exact: true }).click();
    const boxes = page.getByTestId("correction-row-0").getByRole("group", { name: "Supporting evidence" }).getByRole("checkbox");
    await expect(boxes).toHaveCount(6);
  });
});

test.describe("reject (scenario D)", () => {
  test("reject needs reasons and notes, is terminal, and offers no resume", async ({ page }) => {
    await openCase(page, URLS.commit, REJECT_CASE);
    await claimViaUi(page);
    await page.getByRole("button", { name: "Reject", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Reject this invoice?" });
    await expect(dialog).toContainText("terminal for this review case");
    await dialog.getByRole("checkbox", { name: "Duplicate invoice" }).check();
    await expect(dialog.getByRole("button", { name: "Reject invoice" })).toBeDisabled();
    await dialog.getByLabel("Notes (required)").fill("Paid last month under a different number");
    await dialog.getByRole("button", { name: "Reject invoice" }).click();

    await expect(result(page)).toHaveAttribute("data-result", "executed");
    await expect(summary(page)).toContainText("Rejected");
    await expect(page.locator("section[aria-labelledby='section-decisions']")).toContainText("Rejected");
    await expect(page.locator("section[aria-labelledby='section-decisions']")).toContainText("Paid last month");
    await expect(page.getByTestId("resume-panel")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Request workflow resume" })).toHaveCount(0);
  });
});

test.describe("conflicts (scenarios E, F, G)", () => {
  test("a claim that loses a race gets a controlled conflict, nothing is silently retried, and the user must refresh", async ({ page, request }) => {
    await openCase(page, URLS.commit, RACE_CASE);
    await control(request, { race: true }); // another reviewer claims first
    await page.getByRole("button", { name: "Claim this case" }).click();

    const alert = page.getByRole("alert").filter({ hasText: "was not completed" });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText(/Already claimed|no longer open|changed/i);
    await expect(alert).toContainText("Support reference");
    await expect(alert).not.toContainText(/STALE_|CASE_NOT_OPEN|postgres/i);
    await expect(page.getByRole("button", { name: "Claim this case" })).toBeDisabled();
    await expect(page.getByTestId("refresh-required")).toBeVisible();
    expect((await stats(request)).commandPosts).toBe(1);

    await alert.getByRole("button", { name: "Refresh case data" }).click();
    await expect(summary(page)).toContainText("Claimed by another reviewer");
    await expect(workspace(page).getByRole("button", { name: "Claim this case" })).toHaveCount(0);
  });

  test("a stale revision returns 409 copy, keeps the form, and requires a refresh before resubmitting", async ({ page, request }) => {
    await openCase(page, URLS.commit, CLAIM_CASE);
    // Another session changes the case after this page loaded.
    const other = await request.post(`http://127.0.0.1:4322/api/v1/review-cases/${caseId(CLAIM_CASE)}/commands`, {
      headers: { "x-tenant-id": "00000000-0000-0000-0000-000000000000", "x-actor-id": "someone-else", "x-actor-role": "AP_REVIEWER", "x-authenticated-at": new Date().toISOString() },
      data: { command_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", idempotency_key: "other-session-claim-1", action: "CLAIM", disposition: null, observed_review_revision: 1, observed_workflow_revision: 1, reason_codes: [], notes: null, corrections: [], requested_at: new Date().toISOString() },
    });
    expect(other.status()).toBe(200);

    await page.getByRole("button", { name: "Claim this case" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "was not completed" })).toContainText(/changed|no longer open|Already claimed/i);
    await expect(page.getByTestId("refresh-required")).toContainText("nothing is retried automatically");
    await page.getByRole("button", { name: "Refresh case data" }).click();
    await expect(summary(page)).toContainText("Claimed by another reviewer");
  });

  test("an identical retry after a lost response reuses identity and is reported as already recorded", async ({ page }) => {
    await openCase(page, URLS.commit, CLAIM_CASE);
    const bodies: Record<string, unknown>[] = [];
    let dropFirstResponse = true;
    await page.route("**/api/v1/review-cases/*/commands", async (route) => {
      bodies.push(route.request().postDataJSON() as Record<string, unknown>);
      if (dropFirstResponse) {
        dropFirstResponse = false;
        await route.fetch(); // the server executes the command...
        await route.abort("connectionreset"); // ...but the browser never sees the reply
        return;
      }
      await route.continue();
    });

    await page.getByRole("button", { name: "Claim this case" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "was not completed" })).toContainText("Network error");
    await page.getByRole("button", { name: "Claim this case" }).click();
    await expect(result(page)).toHaveAttribute("data-result", "idempotent");
    await expect(result(page)).toContainText("no duplicate was created");

    expect(bodies).toHaveLength(2);
    expect(bodies[1]?.command_id).toBe(bodies[0]?.command_id);
    expect(bodies[1]?.idempotency_key).toBe(bodies[0]?.idempotency_key);
  });

  test("a reused idempotency key with different content is a controlled conflict", async ({ page }) => {
    await openCase(page, URLS.commit, CLAIM_CASE);
    let firstKey: string | null = null;
    await page.route("**/api/v1/review-cases/*/commands", async (route) => {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      if (firstKey === null) firstKey = String(body.idempotency_key);
      else body.idempotency_key = firstKey; // force the key to be reused for different content
      await route.continue({ postData: JSON.stringify(body) });
    });
    await claimViaUi(page);
    await page.getByRole("button", { name: "Release" }).click();
    const alert = page.getByRole("alert").filter({ hasText: "was not completed" });
    await expect(alert).toContainText("Conflicting duplicate request");
  });

  test("double-clicking Claim sends exactly one command", async ({ page, request }) => {
    await openCase(page, URLS.commit, CLAIM_CASE);
    await page.getByRole("button", { name: "Claim this case" }).dblclick();
    await expect(result(page)).toHaveAttribute("data-result", "executed");
    expect((await stats(request)).commandPosts).toBe(1);
  });

  test("reloading after success never resubmits", async ({ page, request }) => {
    await openCase(page, URLS.commit, CLAIM_CASE);
    await claimViaUi(page);
    await page.reload();
    await page.reload();
    expect((await stats(request)).commandPosts).toBe(1);
    await expect(summary(page)).toContainText("Claimed by you");
  });
});

test.describe("failure states", () => {
  test("backend unavailable and database unavailable produce controlled messages", async ({ page, request }) => {
    await openCase(page, URLS.commit, CLAIM_CASE);
    await control(request, { mode: "database_unavailable" });
    await page.getByRole("button", { name: "Claim this case" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "was not completed" })).toContainText("database is temporarily unavailable");
    await expect(result(page)).not.toContainText(/postgres|psycopg|traceback/i);
  });

  test("an unreachable backend at submit time is reported without exposing the host", async ({ page, request }) => {
    await openCase(page, URLS.commit, CLAIM_CASE);
    await control(request, { mode: "unavailable" });
    await page.getByRole("button", { name: "Claim this case" }).click();
    const alert = page.getByRole("alert").filter({ hasText: "was not completed" });
    await expect(alert).toContainText(/could not be reached|timed out|Backend/i);
    await expect(alert).not.toContainText("127.0.0.1");
    // Transport failures are safe to retry, so the control stays enabled.
    await expect(page.getByRole("button", { name: "Claim this case" })).toBeEnabled();
  });

  test("payment-style actions cannot be submitted from the browser at all", async ({ page, request }) => {
    await openCase(page, URLS.commit, CLAIM_CASE);
    let captured: { body: Record<string, unknown>; csrf: string } | null = null;
    await page.route("**/api/v1/review-cases/*/commands", async (route) => {
      captured = { body: route.request().postDataJSON() as Record<string, unknown>, csrf: route.request().headers()["x-csrf-token"] ?? "" };
      await route.abort();
    });
    await page.getByRole("button", { name: "Claim this case" }).click();
    await expect.poll(() => captured !== null).toBe(true);

    for (const action of ["EXECUTE_PAYMENT", "RELEASE_PAYMENT", "BANK_TRANSFER", "POST_TO_ERP"]) {
      const response = await request.post(`${URLS.commit}/api/v1/review-cases/${caseId(CLAIM_CASE)}/commands`, {
        headers: { origin: URLS.commit, "x-csrf-token": captured!.csrf },
        data: { ...captured!.body, action },
      });
      expect(response.status(), action).toBe(422);
      expect(JSON.stringify(await response.json())).toContain("REVIEW_ACTION_NOT_SUPPORTED");
    }
    expect((await stats(request)).commandPosts).toBe(0);
  });
});

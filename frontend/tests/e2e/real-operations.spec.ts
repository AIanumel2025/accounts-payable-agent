import { mkdirSync } from "node:fs";
import path from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

/**
 * M11D Core real FastAPI + separate worker + PostgreSQL + Next.js acceptance.
 * Started by `scripts/run-real-operations.mjs`, which afterwards inspects
 * PostgreSQL directly (`scripts/manage_m11d_tenant.py verify`): the browser
 * assertions below and those database assertions together are the result.
 *
 * The OCR that runs is whatever the worker was started with
 * (`stack.ocrProvider`): "tesseract" = real Tesseract through the Phase 3
 * fallback router (Phase 3 marks those pages REVIEW_REQUIRED by design);
 * "paddleocr" = real PaddleOCR. The spec never pretends otherwise.
 */

interface Stack {
  urls: { admin: string; reviewer: string };
  api: string;
  tenantId: string;
  otherTenantId: string;
  ocrProvider: string;
  fixtures: string;
  artifactRoot: string;
  runtimePassword: string;
}

const stack = JSON.parse(process.env.AP_AGENT_ACCEPTANCE_STACK ?? "null") as Stack | null;
if (stack === null) throw new Error("AP_AGENT_ACCEPTANCE_STACK is required.");
const S: Stack = stack;
const shotsDir = process.env.AP_AGENT_SCREENSHOT_DIR;
if (shotsDir) mkdirSync(shotsDir, { recursive: true });

// `Template1_Instance90.jpg` routes to review under both PaddleOCR and the Tesseract fallback
// (tests/golden/phase_8_expected_results.json); `08181_flat_document.png` completes automatically
// under PaddleOCR only.
const REVIEW_FIXTURE = path.join(S.fixtures, "Template1_Instance90.jpg");
const AUTOMATIC_FIXTURE = path.join(S.fixtures, "08181_flat_document.png");

async function shot(page: Page, name: string) {
  if (shotsDir) await page.screenshot({ path: path.join(shotsDir, `${name}.png`), fullPage: true });
}

function watchConsole(page: Page): string[] {
  const problems: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") problems.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
  return problems;
}

async function pageToken(page: Page): Promise<string> {
  const token = /csrfToken\\?":\\?"([0-9]+\.[A-Za-z0-9_-]+)/.exec(await page.content())?.[1];
  expect(token, "page carries a CSRF token").toBeTruthy();
  return token!;
}

// The file input is disabled until React has hydrated (M11E), so waiting for
// it to become enabled is all that is needed -- no re-selection loop.
async function chooseFile(page: Page, file: string | { name: string; mimeType: string; buffer: Buffer }) {
  const name = typeof file === "string" ? path.basename(file) : file.name;
  await expect(page.getByTestId("file-input")).toBeEnabled();
  await page.getByTestId("file-input").setInputFiles(file);
  await expect(page.getByTestId("selected-file")).toContainText(name);
}

async function upload(page: Page, file: string) {
  await page.goto(`${S.urls.admin}/operations`);
  await chooseFile(page, file);

  const [response] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith("/api/v1/operations/submissions")),
    page.getByTestId("process-button").click(),
  ]);

  return response;
}

async function followJob(page: Page) {
  await page.getByRole("link", { name: "Follow this job" }).click();
  await expect(page.getByTestId("job-detail")).toBeVisible();
}

const jobDetail = (page: Page) => page.getByTestId("job-detail");

test.describe.serial("M11D operations console (real stack)", () => {
  test("empty console, then upload is accepted and queued without running anything in the request", async ({ page }) => {
    const problems = watchConsole(page);
    await page.goto(`${S.urls.admin}/operations`);
    await expect(page.getByRole("heading", { name: "Operations", level: 1 })).toBeVisible();
    await expect(page.getByText("No jobs yet")).toBeVisible();
    await shot(page, "01-operations-upload");

    const response = await upload(page, REVIEW_FIXTURE);

    // The request returned a *queued* job: the worker had not started it (OCR is not in the request).
    expect(response.status(), await response.text()).toBe(202);
    const body = (await response.json()) as { data: { job: { status: string; started_at: string | null; attempt_count: number } } };
    expect(body.data.job.status).toBe("QUEUED");
    expect(body.data.job.started_at).toBeNull();
    expect(body.data.job.attempt_count).toBe(0);
    expect(JSON.stringify(body)).not.toContain(S.artifactRoot);

    await expect(page.getByTestId("submission-result")).toHaveAttribute("data-result", "queued");
    await expect(page.getByTestId("submission-result")).toContainText("has not finished");
    expect(problems).toEqual([]);
  });

  test("the pipeline runs in the worker and the invoice lands in the review queue with a link", async ({ page }) => {
    const problems = watchConsole(page);
    await page.goto(`${S.urls.admin}/operations`);
    await page.getByRole("link", { name: /Template1_Instance90\.jpg/ }).first().click();
    await expect(jobDetail(page)).toBeVisible();

    // The worker picks it up: running (shown when observed), then a terminal outcome. Nothing is optimistic.
    await expect
      .poll(async () => jobDetail(page).getAttribute("data-job-status"), { timeout: 300_000, intervals: [500] })
      .toMatch(/RUNNING|REVIEW_REQUIRED|SUCCEEDED/);
    if ((await jobDetail(page).getAttribute("data-job-status")) === "RUNNING") await shot(page, "02-running-pipeline");

    await expect(jobDetail(page)).toHaveAttribute("data-job-status", "REVIEW_REQUIRED", { timeout: 300_000 });
    await expect(page.getByTestId("job-status")).toContainText("Review required");
    await shot(page, "03-review-required");

    const timeline = page.getByTestId("job-timeline");
    for (const stage of ["Ingestion", "Preprocessing", "OCR", "Normalization", "Financial validation", "Reference matching", "Memory persistence"]) {
      await expect(timeline).toContainText(stage);
    }
    await expect(page.getByTestId("review-link")).toBeVisible();

    await page.getByRole("link", { name: "Open the review case" }).click();
    await expect(page.getByTestId("review-action-workspace")).toBeVisible();
    await expect(page.getByTestId("origin-job-panel")).toBeVisible();
    expect(problems).toEqual([]);
  });

  test("claim, approve and request resume; the worker resumes from the recorded stage and completes", async ({ page }) => {
    const problems = watchConsole(page);
    await page.goto(`${S.urls.admin}/operations`);
    await page.getByRole("link", { name: /Template1_Instance90\.jpg/ }).first().click();
    await page.getByRole("link", { name: "Open the review case" }).click();
    const workspace = page.getByTestId("review-action-workspace");
    await expect(workspace).toBeVisible();

    await page.getByRole("button", { name: "Claim this case" }).click();
    await expect(page.getByTestId("command-result")).toHaveAttribute("data-result", "executed");

    await page.getByRole("button", { name: "Approve", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Approve this invoice?" });
    await dialog.getByRole("checkbox", { name: "Reviewer verified the invoice" }).check();
    await dialog.getByRole("button", { name: "Approve invoice" }).click();
    await expect(page.getByTestId("command-result")).toHaveAttribute("data-result", "executed");

    await expect(page.getByTestId("resume-panel")).toContainText("queues a worker job");
    await page.getByRole("button", { name: "Request workflow resume" }).click();
    await expect(page.getByTestId("resume-restart-stage")).toHaveText("Memory persistence");
    await expect(page.getByTestId("command-result")).toContainText("worker job was queued");

    // The handoff is not the execution: the job panel distinguishes the worker's states.
    const panel = page.getByTestId("resume-job-panel");
    await expect(panel).toBeVisible();
    await shot(page, "04-review-action-and-resume");

    await expect(panel).toHaveAttribute("data-job-status", "SUCCEEDED", { timeout: 240_000 });
    await expect(page.getByTestId("resume-job-stages")).toContainText("Restarted at Memory persistence");
    await expect(page.getByTestId("resume-job-stages")).toContainText("ran Memory persistence");
    await shot(page, "05-completed");

    await panel.getByRole("link", { name: "View the job and its timeline" }).click();
    await expect(page.getByTestId("job-resume")).toContainText("Memory persistence");
    await expect(page.getByTestId("resume-executed")).toHaveText("Memory persistence");
    await expect(page.getByTestId("job-status")).toContainText("Completed");

    // The approved case left the review queue.
    await page.goto(`${S.urls.admin}/review-queue`);
    await expect(page.getByText("Template1_Instance90.jpg")).toHaveCount(0);
    expect(problems).toEqual([]);
  });

  test("automatic completion (only when a worker OCR provider can produce a clean read)", async ({ page }) => {
    test.skip(S.ocrProvider !== "paddleocr", `Needs PaddleOCR; this run's worker used "${S.ocrProvider}", whose Phase 3 fallback always routes to review. Covered locally by the backend shim test.`);

    const response = await upload(page, AUTOMATIC_FIXTURE);
    expect(response.status()).toBe(202);
    await followJob(page);
    await expect(jobDetail(page)).toHaveAttribute("data-job-status", "SUCCEEDED", { timeout: 420_000 });
    await expect(page.getByTestId("job-status")).toContainText("Completed automatically");
    await expect(page.getByTestId("review-link")).toHaveCount(0);
  });

  test("an AP_REVIEWER cannot submit; the refusal is explained and nothing is stored", async ({ page }) => {
    await page.goto(`${S.urls.reviewer}/operations`);
    await chooseFile(page, AUTOMATIC_FIXTURE);
    await page.getByTestId("process-button").click();

    await expect(page.getByTestId("submission-result")).toHaveAttribute("data-result", "error");
    await expect(page.getByTestId("submission-result")).toContainText("not permitted to submit");
  });

  test("the boundary refuses forged, identity-bearing and payment-shaped uploads", async ({ page, request }) => {
    await page.goto(`${S.urls.admin}/operations`);
    const token = await pageToken(page);
    const url = `${S.urls.admin}/api/v1/operations/submissions`;
    const good = { name: "a.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\n%%EOF\n") };

    const noToken = await request.post(url, { headers: { origin: S.urls.admin }, multipart: { operation_id: crypto.randomUUID(), file: good } });
    expect(noToken.status()).toBe(403);

    const crossOrigin = await request.post(url, { headers: { origin: "http://evil.example", "x-csrf-token": token }, multipart: { operation_id: crypto.randomUUID(), file: good } });
    expect(crossOrigin.status()).toBe(403);

    for (const [field, code] of [["tenant_id", "FIELD_NOT_ALLOWED"], ["actor_role", "FIELD_NOT_ALLOWED"], ["payment_amount", "PAYMENT_FIELD_PROHIBITED"], ["bank_account", "PAYMENT_FIELD_PROHIBITED"]] as const) {
      const response = await request.post(url, { headers: { origin: S.urls.admin, "x-csrf-token": token }, multipart: { operation_id: crypto.randomUUID(), file: good, [field]: "x" } });
      expect(response.status(), field).toBe(422);
      expect(((await response.json()) as { errors: string[] }).errors).toEqual([code]);
    }

    const traversal = await request.post(url, { headers: { origin: S.urls.admin, "x-csrf-token": token }, multipart: { operation_id: crypto.randomUUID(), file: { ...good, name: "../../etc/passwd.pdf" } } });
    expect(traversal.status()).toBe(422);
    expect(((await traversal.json()) as { errors: string[] }).errors).toEqual(["FILENAME_INVALID"]);

    const unsupported = await request.post(url, { headers: { origin: S.urls.admin, "x-csrf-token": token }, multipart: { operation_id: crypto.randomUUID(), file: { name: "a.exe", mimeType: "application/octet-stream", buffer: Buffer.from("MZ") } } });
    expect(unsupported.status()).toBe(422);

    const mismatched = await request.post(url, { headers: { origin: S.urls.admin, "x-csrf-token": token }, multipart: { operation_id: crypto.randomUUID(), file: { name: "a.png", mimeType: "image/png", buffer: Buffer.from("%PDF-1.4\n%%EOF\n") } } });
    expect(mismatched.status()).toBe(415);

    // Read-only proxy: the submission endpoint is unreachable through it.
    expect((await request.get(`${S.urls.admin}/api/backend/api/v1/operations/submissions`)).status()).toBe(404);
  });

  test("no secret, path, tenant id or credential reaches the browser", async ({ page, request }) => {
    const scripts: string[] = [];
    page.on("response", async (response) => {
      if (response.url().includes("/_next/static/") && response.url().endsWith(".js")) scripts.push(await response.text());
    });
    await page.goto(`${S.urls.admin}/operations`);
    await page.getByRole("link", { name: /Template1_Instance90\.jpg/ }).first().click();
    await expect(jobDetail(page)).toBeVisible();

    const pages = [await page.content(), await (await request.get(`${S.urls.admin}/operations`)).text(), ...scripts];
    const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, cookie: document.cookie }));
    const haystack = [...pages, storage].join("\n");

    for (const forbidden of [S.artifactRoot, S.tenantId, S.otherTenantId, S.runtimePassword, "artifact://", "AP_AGENT_", "postgresql://"]) {
      expect(haystack, `browser-visible content must not contain ${forbidden === S.runtimePassword ? "the runtime credential" : forbidden}`).not.toContain(forbidden);
    }
  });

  test("accessibility: no serious or critical automated violations on the operations pages", async ({ page }) => {
    const scan = async (label: string) => {
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag22aa"]).analyze();
      const seriousOrCritical = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
      expect(seriousOrCritical, `${label}: ${JSON.stringify(seriousOrCritical, null, 2)}`).toEqual([]);
    };

    for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      await page.goto(`${S.urls.admin}/operations`);
      await expect(page.getByTestId("upload-form")).toBeVisible();
      await scan(`operations ${viewport.width}px`);

      // With a file selected and a validation error showing.
      await expect(page.getByTestId("file-input")).toBeEnabled();
      await page.getByTestId("file-input").setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("x") });
      await expect(page.getByTestId("file-error")).toBeVisible();
      await scan(`operations with validation error ${viewport.width}px`);

      await page.goto(`${S.urls.admin}/operations`);
      // The upload job (not the later resume job) carries the review-case link.
      await page
        .locator('[data-testid="job-row"]:visible, [data-testid="job-cards"] li:visible')
        .filter({ hasText: "Invoice processing" })
        .getByRole("link", { name: /Template1_Instance90\.jpg/ })
        .first()
        .click();
      await expect(jobDetail(page)).toBeVisible();
      await scan(`job detail ${viewport.width}px`);

      await page.getByRole("link", { name: "Open the review case" }).click();
      await expect(page.getByTestId("origin-job-panel").or(page.getByTestId("resume-job-panel"))).toBeVisible();
      await scan(`review case with job panel ${viewport.width}px`);
    }
  });

  test("keyboard: the file can be chosen, submitted and removed without a pointer", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(`${S.urls.admin}/operations`);

    await expect(page.getByTestId("file-input")).toBeEnabled();
    await page.getByTestId("file-input").focus();
    await expect(page.getByTestId("file-input")).toBeFocused();
    await page.getByTestId("file-input").setInputFiles(AUTOMATIC_FIXTURE);
    await expect(page.getByTestId("operations-announcer")).toContainText("Selected");
    await expect(page.getByTestId("operations-announcer")).toContainText("Selected 08181_flat_document.png.");

    await page.keyboard.press("Tab");
    await expect(page.getByTestId("process-button")).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByTestId("remove-file")).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("selected-file")).toHaveCount(0);
    await expect(page.getByTestId("operations-announcer")).toContainText("File removed.");
  });

  test("mobile layout: no horizontal overflow on the operations page and job detail", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${S.urls.admin}/operations`);
    await expect(page.getByTestId("job-cards")).toBeVisible();
    await shot(page, "06-mobile-operations");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

    await page.getByTestId("job-cards").getByRole("link").first().click();
    await expect(jobDetail(page)).toBeVisible();
    await shot(page, "07-mobile-job-detail");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
});

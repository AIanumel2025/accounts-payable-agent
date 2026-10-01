import { mkdirSync } from "node:fs";
import path from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";

/**
 * M11E hosted-mode acceptance (real FastAPI in `clerk_jwt` mode + object storage + separate
 * worker + PostgreSQL + Next.js in `clerk_jwt` mode). Started by
 * `scripts/run-hosted-acceptance.mjs`, which afterwards inspects the object store and
 * PostgreSQL directly.
 *
 * Authentication here is real in everything except the identity provider itself: the session
 * cookies are signed by a test key, verified by Clerk's own middleware (networkless public
 * key) and independently by FastAPI against a local JWKS endpoint, and the tenant and role come
 * from the database identity mapping. Real Clerk sign-in, real R2 and real Render are validated
 * separately in the hosted acceptance. The spec never pretends otherwise.
 */

type Cookie = { name: string; value: string; url: string };

interface Stack {
  web: string;
  api: string;
  tenantId: string;
  otherTenantId: string;
  orgNames: { a: string; b: string };
  ocrProvider: string;
  fixtures: string;
  cookies: Record<string, Cookie[]>;
  secrets: Record<string, string>;
}

const stack = JSON.parse(process.env.AP_AGENT_ACCEPTANCE_STACK ?? "null") as Stack | null;
if (stack === null) throw new Error("AP_AGENT_ACCEPTANCE_STACK is required.");
const S: Stack = stack;
const shotsDir = process.env.AP_AGENT_SCREENSHOT_DIR;
if (shotsDir) mkdirSync(shotsDir, { recursive: true });

const REVIEW_FIXTURE = path.join(S.fixtures, "Template1_Instance90.jpg");
const FIXTURE_NAME = "Template1_Instance90.jpg";

const sessionToken = (persona: string) => S.cookies[persona]!.find((cookie) => cookie.name === "__session")!.value;

async function shot(page: Page, name: string) {
  if (shotsDir) await page.screenshot({ path: path.join(shotsDir, `${name}.png`), fullPage: true });
}

/** A browser session for one persona. Requests to the (unreachable) identity-provider host are dropped so logs stay quiet. */
async function session(browser: Browser, persona: string | null, viewport = { width: 1280, height: 900 }): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({ viewport });
  if (persona !== null) await context.addCookies(S.cookies[persona]!);
  await context.route(/clerk\.accounts\.example\.test/, (route) => route.abort());
  return { context, page: await context.newPage() };
}

async function uploadFixture(page: Page) {
  await page.goto(`${S.web}/operations`);
  await expect(page.getByTestId("file-input")).toBeEnabled();
  await page.getByTestId("file-input").setInputFiles(REVIEW_FIXTURE);
  await expect(page.getByTestId("selected-file")).toContainText(FIXTURE_NAME);
  const started = Date.now();
  const [response] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith("/api/v1/operations/submissions")),
    page.getByTestId("process-button").click(),
  ]);
  return { response, elapsedMs: Date.now() - started };
}

test.describe.serial("M11E hosted workflow (Clerk-style authentication, object storage, worker)", () => {
  test("unauthenticated visitors are redirected to sign-in; the API surface answers 401 without a redirect", async ({ browser }) => {
    const { context, page } = await session(browser, null);
    for (const target of ["/dashboard", "/review-queue", "/operations", "/operations/jobs/77777777-7777-4777-8777-777777777777", "/review-cases/77777777-7777-4777-8777-777777777777"]) {
      await page.goto(`${S.web}${target}`);
      expect(new URL(page.url()).pathname, target).toBe("/sign-in");
    }
    await page.goto(`${S.web}/dashboard`);
    expect(new URL(page.url()).searchParams.get("redirect_url")).toBe("/dashboard");
    await shot(page, "01-sign-in-redirect");

    const read = await context.request.get(`${S.web}/api/backend/api/v1/dashboard`);
    expect(read.status()).toBe(401);
    expect(((await read.json()) as { errors: string[] }).errors).toEqual(["AUTHENTICATION_REQUIRED"]);
    const write = await context.request.post(`${S.web}/api/v1/operations/submissions`, { headers: { origin: S.web }, multipart: { operation_id: crypto.randomUUID(), file: { name: "a.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\n%%EOF\n") } } });
    expect(write.status()).toBe(401);
    const command = await context.request.post(`${S.web}/api/v1/review-cases/77777777-7777-4777-8777-777777777777/commands`, { headers: { origin: S.web, "content-type": "application/json" }, data: {} });
    expect(command.status()).toBe(401);
    await context.close();
  });

  test("forged and tampered sessions are not signed in", async ({ browser }) => {
    for (const persona of ["tampered", "wrongAuthorizedParty"]) {
      const { context, page } = await session(browser, persona);
      await page.goto(`${S.web}/dashboard`);
      expect(new URL(page.url()).pathname, persona).toBe("/sign-in");
      await context.close();
    }
    // Prototype headers sent by a browser change nothing.
    const { context } = await session(browser, null);
    const forged = await context.request.get(`${S.web}/api/backend/api/v1/dashboard`, { headers: { "X-Tenant-ID": S.tenantId, "X-Actor-Role": "TENANT_ADMIN", "X-Actor-ID": "attacker" } });
    expect(forged.status()).toBe(401);
    await context.close();
  });

  test("FastAPI rejects bad tokens by itself, whatever the front end did", async ({ request }) => {
    const get = (headers: Record<string, string>) => request.get(`${S.api}/api/v1/session`, { headers });
    expect((await get({})).status()).toBe(401);
    expect((await get({ "X-Tenant-ID": S.tenantId, "X-Actor-ID": "a", "X-Actor-Role": "TENANT_ADMIN" })).status()).toBe(401);

    const tampered = await get({ Authorization: `Bearer ${sessionToken("tampered")}` });
    expect(tampered.status()).toBe(401);
    expect(((await tampered.json()) as { errors: string[] }).errors).toEqual(["TOKEN_SIGNATURE_INVALID"]);

    const wrongParty = await get({ Authorization: `Bearer ${sessionToken("wrongAuthorizedParty")}` });
    expect(((await wrongParty.json()) as { errors: string[] }).errors).toEqual(["TOKEN_AUTHORIZED_PARTY_INVALID"]);

    const noOrg = await get({ Authorization: `Bearer ${sessionToken("noOrganization")}` });
    expect(((await noOrg.json()) as { errors: string[] }).errors).toEqual(["ORGANIZATION_REQUIRED"]);

    const unmapped = await get({ Authorization: `Bearer ${sessionToken("unmapped")}` });
    expect(unmapped.status()).toBe(403);
    expect(((await unmapped.json()) as { errors: string[] }).errors).toEqual(["IDENTITY_NOT_MAPPED"]);

    const inactive = await get({ Authorization: `Bearer ${sessionToken("inactive")}` });
    expect(((await inactive.json()) as { errors: string[] }).errors).toEqual(["IDENTITY_MEMBERSHIP_INACTIVE"]);

    // Roles come from the database mapping, never from the token or a header.
    const forgedRole = await get({ Authorization: `Bearer ${sessionToken("auditor")}`, "X-Actor-Role": "TENANT_ADMIN" });
    expect(((await forgedRole.json()) as { data: { role: string } }).data.role).toBe("READ_ONLY_AUDITOR");
  });

  test("a signed-in user without an active organization must choose one", async ({ browser }) => {
    const { context, page } = await session(browser, "noOrganization");
    await page.goto(`${S.web}/dashboard`);
    expect(new URL(page.url()).pathname).toBe("/organization-required");
    await expect(page.getByTestId("account-state")).toHaveAttribute("data-reason", "no-organization");
    await expect(page.getByRole("heading", { level: 1, name: "Choose an organization" })).toBeVisible();
    await shot(page, "02-organization-required");

    const api = await context.request.get(`${S.web}/api/backend/api/v1/dashboard`);
    expect(api.status()).toBe(403);
    expect(((await api.json()) as { errors: string[] }).errors).toEqual(["ORGANIZATION_REQUIRED"]);
    await context.close();
  });

  test("unmapped and inactive accounts see a clear account state, not application data", async ({ browser }) => {
    for (const [persona, reason, heading] of [["unmapped", "not-mapped", "This account is not set up yet"], ["inactive", "inactive", "Your access is inactive"]] as const) {
      const { context, page } = await session(browser, persona);
      await page.goto(`${S.web}/dashboard`);
      // The redirect is issued while the page streams (loading state first), so wait for it.
      await page.waitForURL(/\/access-denied\?reason=/, { timeout: 30_000 });
      expect(new URL(page.url()).pathname).toBe("/access-denied");
      expect(new URL(page.url()).searchParams.get("reason")).toBe(reason);
      await expect(page.getByTestId("account-state")).toHaveAttribute("data-reason", reason);
      await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
      await expect(page.getByTestId("dashboard-body")).toHaveCount(0);
      await shot(page, `03-account-state-${reason}`);
      await context.close();
    }
  });

  test("an operator sees their organization and role, and an upload is accepted and queued without running OCR in the request", async ({ browser }) => {
    const { context, page } = await session(browser, "operator");
    await page.goto(`${S.web}/operations`);
    await expect(page.getByRole("heading", { name: "Operations", level: 1 })).toBeVisible();
    await expect(page.getByTestId("tenant-name")).toContainText("M11D");
    await expect(page.getByText("AP Operator", { exact: true }).first()).toBeVisible();
    await expect(page.getByTestId("sign-out")).toBeVisible();
    await shot(page, "04-shell-and-operations");

    const { response, elapsedMs } = await uploadFixture(page);
    expect(response.status(), await response.text()).toBe(202);
    const body = (await response.json()) as { data: { job: { status: string; started_at: string | null; attempt_count: number } } };
    expect(body.data.job).toMatchObject({ status: "QUEUED", started_at: null, attempt_count: 0 });
    // Queued, not processed: far quicker than any OCR run.
    expect(elapsedMs).toBeLessThan(20_000);
    expect(JSON.stringify(body)).not.toMatch(/object:\/\/|tenants\/|sha256/);
    await expect(page.getByTestId("submission-result")).toHaveAttribute("data-result", "queued");
    await shot(page, "05-upload-queued");
    await context.close();
  });

  test("the worker reads the object and runs the pipeline; the job lands in review", async ({ browser }) => {
    const { context, page } = await session(browser, "operator");
    await page.goto(`${S.web}/operations`);
    await page.getByRole("link", { name: new RegExp(FIXTURE_NAME.replace(".", "\\.")) }).first().click();
    await expect(page.getByTestId("job-detail")).toBeVisible();
    await expect.poll(async () => page.getByTestId("job-detail").getAttribute("data-job-status"), { timeout: 300_000, intervals: [500] }).toMatch(/RUNNING|REVIEW_REQUIRED|SUCCEEDED/);
    if ((await page.getByTestId("job-detail").getAttribute("data-job-status")) === "RUNNING") await shot(page, "06-processing");
    await expect(page.getByTestId("job-detail")).toHaveAttribute("data-job-status", "REVIEW_REQUIRED", { timeout: 300_000 });
    await expect(page.getByTestId("job-timeline")).toContainText("Upload verified");
    await shot(page, "07-review-required");
    await context.close();
  });

  test("a reviewer claims, approves and requests resume; the worker resumes from the recorded stage and completes", async ({ browser }) => {
    const { context, page } = await session(browser, "reviewer");
    await page.goto(`${S.web}/operations`);
    await page.getByRole("link", { name: new RegExp(FIXTURE_NAME.replace(".", "\\.")) }).first().click();
    await page.getByRole("link", { name: "Open the review case" }).click();
    await expect(page.getByTestId("review-action-workspace")).toBeVisible();
    await expect(page.getByText("AP Reviewer", { exact: true }).first()).toBeVisible();

    await page.getByRole("button", { name: "Claim this case" }).click();
    await expect(page.getByTestId("command-result")).toHaveAttribute("data-result", "executed");
    await shot(page, "08-review-state");

    await page.getByRole("button", { name: "Approve", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Approve this invoice?" });
    await dialog.getByRole("checkbox", { name: "Reviewer verified the invoice" }).check();
    await dialog.getByRole("button", { name: "Approve invoice" }).click();
    await expect(page.getByTestId("command-result")).toHaveAttribute("data-result", "executed");

    await page.getByRole("button", { name: "Request workflow resume" }).click();
    await expect(page.getByTestId("resume-restart-stage")).toHaveText("Memory persistence");
    const panel = page.getByTestId("resume-job-panel");
    await expect(panel).toHaveAttribute("data-job-status", "SUCCEEDED", { timeout: 240_000 });
    await expect(page.getByTestId("resume-job-stages")).toContainText("Restarted at Memory persistence");
    await shot(page, "09-completed");

    // An authenticated browser refresh keeps the session and the state.
    await page.reload();
    await expect(page.getByTestId("resume-job-panel")).toHaveAttribute("data-job-status", "SUCCEEDED");
    await context.close();
  });

  test("a read-only auditor can follow jobs but cannot submit", async ({ browser }) => {
    const { context, page } = await session(browser, "auditor");
    await page.goto(`${S.web}/operations`);
    await expect(page.getByText("Read-Only Auditor", { exact: true }).first()).toBeVisible();
    await expect(page.getByTestId("job-table")).toContainText(FIXTURE_NAME);
    await expect(page.getByTestId("file-input")).toBeEnabled();
    await page.getByTestId("file-input").setInputFiles(REVIEW_FIXTURE);
    await page.getByTestId("process-button").click();
    await expect(page.getByTestId("submission-result")).toContainText("not permitted to submit");
    await context.close();
  });

  test("another organization sees nothing of this one", async ({ browser }) => {
    const { context, page } = await session(browser, "otherAdmin");
    await page.goto(`${S.web}/operations`);
    await expect(page.getByText("No jobs yet")).toBeVisible();
    await expect(page.getByTestId("tenant-name")).toContainText("M11D");
    const listing = await context.request.get(`${S.web}/api/backend/api/v1/operations/jobs`);
    expect(((await listing.json()) as { data: { items: unknown[] } }).data.items).toEqual([]);
    const mine = await (await (await browser.newContext()).request.get(`${S.api}/api/v1/operations/jobs`, { headers: { Authorization: `Bearer ${sessionToken("admin")}` } })).json() as { data: { items: { job_id: string }[] } };
    expect(mine.data.items.length).toBeGreaterThan(0);
    const foreign = await context.request.get(`${S.web}/api/backend/api/v1/operations/jobs/${mine.data.items[0]!.job_id}`);
    expect(foreign.status()).toBe(404);
    await shot(page, "10-other-organization");
    await context.close();
  });

  test("hydration race: a file chosen before React hydrates is never silently lost", async ({ browser }) => {
    const { context, page } = await session(browser, "operator");
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    await context.route("**/_next/static/**", async (route) => {
      await gate;
      await route.continue();
    });

    await page.goto(`${S.web}/operations`, { waitUntil: "commit" });
    const input = page.getByTestId("file-input");
    await expect(input).toBeAttached({ timeout: 30_000 });
    await expect(input).toBeDisabled(); // unavailable until it is functional

    let chosenEarly = true;
    await input.setInputFiles(REVIEW_FIXTURE, { timeout: 2_000 }).catch(() => (chosenEarly = false));

    release();
    await expect(input).toBeEnabled();
    if (chosenEarly) {
      await expect(page.getByTestId("selected-file")).toContainText(FIXTURE_NAME);
      await expect(page.getByTestId("process-button")).toBeEnabled();
    } else {
      await input.setInputFiles(REVIEW_FIXTURE);
      await expect(page.getByTestId("selected-file")).toContainText(FIXTURE_NAME);
    }
    await context.close();
  });

  test("sign-out control is present and an ended session is no longer signed in", async ({ browser }) => {
    const { context, page } = await session(browser, "operator");
    await page.goto(`${S.web}/dashboard`);
    await expect(page.getByTestId("sign-out")).toBeVisible();
    await expect(page.getByTestId("sign-out")).toHaveAccessibleName("Sign out");

    // What Clerk's sign-out does to the browser: the session cookies are cleared.
    await context.clearCookies();
    await page.reload();
    expect(new URL(page.url()).pathname).toBe("/sign-in");
    await context.close();
  });

  test("no secret, token, private hostname or storage credential reaches the browser", async ({ browser }) => {
    const { context, page } = await session(browser, "operator");
    const scripts: string[] = [];
    page.on("response", async (response) => {
      if (response.url().includes("/_next/static/") && /\.(js|css)$/.test(response.url())) scripts.push(await response.text().catch(() => ""));
    });
    await page.goto(`${S.web}/operations`);
    await expect(page.getByTestId("tenant-name")).toBeVisible();
    await page.getByRole("link", { name: new RegExp(FIXTURE_NAME.replace(".", "\\.")) }).first().click();
    await expect(page.getByTestId("job-detail")).toBeVisible();

    const rendered = [await page.content(), await (await context.request.get(`${S.web}/operations`)).text(), ...scripts];
    const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
    const haystack = [...rendered, storage].join("\n");

    const forbidden = [
      S.secrets.clerkSecretKey, S.secrets.runtimePassword, S.secrets.s3Endpoint, S.secrets.bucket, S.secrets.apiHost, S.secrets.jwksHost,
      S.tenantId, S.otherTenantId, S.orgNames.a, "AP_AGENT_", "postgresql://", "object://", "tenants/", "CLERK_SECRET_KEY",
      ...Object.keys(S.cookies).map(sessionToken),
    ];
    for (const value of forbidden) expect(haystack, "browser-visible content contains a forbidden value").not.toContain(value);
    await context.close();
  });

  test("accessibility: no serious or critical violations on the hosted pages (desktop and mobile)", async ({ browser }) => {
    for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
      for (const [persona, target] of [["operator", "/operations"], ["reviewer", "/review-queue"], ["unmapped", "/dashboard"], ["noOrganization", "/dashboard"]] as const) {
        const { context, page } = await session(browser, persona, viewport);
        await page.goto(`${S.web}${target}`);
        await expect(page.locator("main")).toBeVisible();
        const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag22aa"]).analyze();
        const seriousOrCritical = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
        expect(seriousOrCritical, `${persona} ${target} ${viewport.width}px: ${JSON.stringify(seriousOrCritical, null, 2)}`).toEqual([]);
        if (viewport.width === 390) {
          expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `${persona} ${target}: horizontal overflow`).toBe(true);
          if (persona === "operator") await shot(page, "11-mobile-operations");
          if (persona === "unmapped") await shot(page, "12-mobile-account-state");
        }
        await context.close();
      }
    }
  });
});

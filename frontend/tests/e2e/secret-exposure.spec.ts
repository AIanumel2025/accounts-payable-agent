import { expect, test } from "@playwright/test";
import { setMockBackendMode } from "./support/control";

/**
 * M11A task §16: browser output and built static assets must never contain
 * server-only secrets/config. This suite checks the live rendered page,
 * page source, browser storage, cookies, and console output; a companion
 * filesystem scan of `.next/static` runs separately via
 * `scripts/check-build-secrets.mjs` after `npm run build` (that script
 * cannot run inside a browser context).
 */

const FORBIDDEN_PATTERNS = [
  /playwright-e2e/i, // the configured AP_AGENT_DEV_ACTOR_ID for this test run
  /00000000-0000-0000-0000-000000000000/, // the configured AP_AGENT_DEV_TENANT_ID
  /READ_ONLY_AUDITOR/, // server-only role configuration value, distinct from its humanized UI label
  /postgres(?:ql)?:\/\//i,
  /neon\.tech/i,
  /AP_AGENT_POSTGRES_DSN/,
  /AP_AGENT_TEST_POSTGRES_DSN/,
];

test.beforeEach(async ({ request }) => {
  await setMockBackendMode(request, "normal");
});

test.describe("secret exposure", () => {
  test("rendered page source contains no server-only secret values", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByTestId("dashboard-body")).toBeVisible();

    const html = await page.content();
    for (const pattern of FORBIDDEN_PATTERNS) {
      expect(html, `page HTML matched forbidden pattern ${pattern}`).not.toMatch(pattern);
    }
  });

  test("no secret value is reachable from evaluated browser JavaScript state", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByTestId("dashboard-body")).toBeVisible();

    const serialized = await page.evaluate(() => {
      try {
        return JSON.stringify({
          html: document.documentElement.outerHTML,
          scripts: Array.from(document.scripts).map((s) => s.textContent ?? ""),
        });
      } catch {
        return "";
      }
    });

    for (const pattern of FORBIDDEN_PATTERNS) {
      expect(serialized, `browser JS state matched forbidden pattern ${pattern}`).not.toMatch(pattern);
    }
  });

  test("no secret value is stored in localStorage, sessionStorage, or cookies", async ({ page, context }) => {
    await page.goto("/dashboard");
    await expect(page.getByTestId("dashboard-body")).toBeVisible();

    const storageDump = await page.evaluate(() => {
      const local: Record<string, string> = {};
      const session: Record<string, string> = {};
      for (let i = 0; i < localStorage.length; i += 1) {
        const key = localStorage.key(i)!;
        local[key] = localStorage.getItem(key) ?? "";
      }
      for (let i = 0; i < sessionStorage.length; i += 1) {
        const key = sessionStorage.key(i)!;
        session[key] = sessionStorage.getItem(key) ?? "";
      }
      return JSON.stringify({ local, session });
    });

    const cookies = await context.cookies();
    const cookieDump = JSON.stringify(cookies);

    for (const pattern of FORBIDDEN_PATTERNS) {
      expect(storageDump).not.toMatch(pattern);
      expect(cookieDump).not.toMatch(pattern);
    }
  });

  test("console output contains no server-only secret values", async ({ page }) => {
    const consoleMessages: string[] = [];
    page.on("console", (msg) => consoleMessages.push(msg.text()));

    await page.goto("/dashboard");
    await expect(page.getByTestId("dashboard-body")).toBeVisible();

    const combined = consoleMessages.join("\n");
    for (const pattern of FORBIDDEN_PATTERNS) {
      expect(combined, `console output matched forbidden pattern ${pattern}`).not.toMatch(pattern);
    }
  });

  test("no uncaught browser console errors on a normal dashboard load", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    page.on("console", (msg) => {
      if (msg.type() === "error") errors.push(msg.text());
    });

    await page.goto("/dashboard");
    await expect(page.getByTestId("dashboard-body")).toBeVisible();

    expect(errors, `unexpected console errors: ${errors.join("; ")}`).toEqual([]);
  });

  // M11B task §14.
  test("review queue page source and browser JS state contain no server-only secret values", async ({ page }) => {
    await page.goto("/review-queue");
    await expect(page.getByTestId("queue-table")).toBeVisible();

    const html = await page.content();
    for (const pattern of FORBIDDEN_PATTERNS) {
      expect(html, `page HTML matched forbidden pattern ${pattern}`).not.toMatch(pattern);
    }
  });

  test("review case detail page source contains no server-only secret values or local filesystem paths", async ({ page }) => {
    await page.goto("/review-cases/cccccccc-0000-4000-8000-000000000003");
    await expect(page.getByTestId("review-case-detail-body")).toBeVisible();

    const html = await page.content();
    for (const pattern of FORBIDDEN_PATTERNS) {
      expect(html, `page HTML matched forbidden pattern ${pattern}`).not.toMatch(pattern);
    }
    expect(html).not.toMatch(/\/home\/|\/tmp\//);
  });

  test("no uncaught browser console errors on a normal review-queue or detail-page load", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    page.on("console", (msg) => {
      if (msg.type() === "error") errors.push(msg.text());
    });

    await page.goto("/review-queue");
    await expect(page.getByTestId("queue-table")).toBeVisible();
    await page.getByRole("link", { name: "Open review case for Template1_Instance90.jpg" }).click();
    await expect(page.getByTestId("review-case-detail-body")).toBeVisible();

    expect(errors, `unexpected console errors: ${errors.join("; ")}`).toEqual([]);
  });
});

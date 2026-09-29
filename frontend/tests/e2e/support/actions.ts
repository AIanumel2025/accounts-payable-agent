import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const URLS = {
  commit: "http://127.0.0.1:4321",
  validation: "http://127.0.0.1:4323",
  disabled: "http://127.0.0.1:4324",
  auditor: "http://127.0.0.1:4325",
} as const;
export const MOCK = "http://127.0.0.1:4322";

/** Case n (1..7) seeded by `mock-backend-actions.mjs`. */
export function caseId(n: number): string {
  return `1111111${n}-0000-4000-8000-0000000000${String(n).padStart(2, "0")}`;
}
export const CLAIM_CASE = 1;
export const APPROVE_CASE = 2;
export const CORRECT_CASE = 3;
export const REJECT_CASE = 4;
export const RACE_CASE = 5;
export const OTHER_CASE = 6;
export const RESUME_CASE = 7;

export async function resetMock(request: APIRequestContext) {
  const response = await request.post(`${MOCK}/__reset`);
  expect(response.ok()).toBe(true);
}

export async function control(request: APIRequestContext, body: Record<string, unknown>) {
  const response = await request.post(`${MOCK}/__control`, { data: body });
  expect(response.ok()).toBe(true);
}

export interface MockStats {
  commandPosts: number;
  lastCommand: { headers: Record<string, string | null>; body: Record<string, unknown> | null } | null;
}

export async function stats(request: APIRequestContext): Promise<MockStats> {
  const response = await request.get(`${MOCK}/__stats`);
  return (await response.json()) as MockStats;
}

/** Collects console errors and uncaught page errors for the "no unexpected console errors" assertions. */
export function watchConsole(page: Page): string[] {
  const problems: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") problems.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
  // Name the URL behind any "Failed to load resource" console line, so a stray 4xx is diagnosable.
  page.on("response", (response) => {
    if (response.status() >= 400) problems.push(`http ${response.status()}: ${new URL(response.url()).pathname}`);
  });
  return problems;
}

export async function openCase(page: Page, base: string, n: number) {
  await page.goto(`${base}/review-cases/${caseId(n)}`);
  await expect(page.getByTestId("review-action-workspace")).toBeVisible();
}

export const workspace = (page: Page) => page.getByTestId("review-action-workspace");
export const result = (page: Page) => page.getByTestId("command-result");
export const summary = (page: Page) => page.getByTestId("action-summary");

/** Claims the open case as the configured reviewer through the real UI. */
export async function claimViaUi(page: Page) {
  await page.getByRole("button", { name: "Claim this case" }).click();
  await expect(result(page)).toHaveAttribute("data-result", "executed");
  await expect(summary(page)).toContainText("Claimed by you");
}

/** Fills the first (and only) correction row. */
export async function fillCorrection(
  page: Page,
  options: { target: string; value: string; reason?: string; evidence?: string; reasonCode?: string },
) {
  const row = page.getByTestId("correction-row-0");
  await row.getByLabel("Field to correct").selectOption(options.target);
  await row.getByLabel("Corrected value").fill(options.value);
  await row.getByLabel("Reason for this correction").fill(options.reason ?? "Verified against the source document");
  if (options.evidence !== "") await row.getByRole("checkbox", { name: new RegExp(options.evidence ?? "ev-po-1") }).check();
  await page.getByRole("group", { name: "Reason codes" }).getByRole("checkbox", { name: options.reasonCode ?? "Verified against evidence" }).check();
}

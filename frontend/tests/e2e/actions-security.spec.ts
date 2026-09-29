import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { CLAIM_CASE, MOCK, URLS, caseId, control, openCase, resetMock, stats, watchConsole } from "./support/actions";

test.beforeEach(async ({ request }) => resetMock(request));

const COMMAND_URL = (base: string, id = caseId(CLAIM_CASE)) => `${base}/api/v1/review-cases/${id}/commands`;

/** Loads the case page and captures the exact request + CSRF token the real UI would send (without letting it through). */
async function captureUiRequest(page: Page) {
  await openCase(page, URLS.commit, CLAIM_CASE);
  let captured: { body: Record<string, unknown>; csrf: string } | null = null;
  await page.route("**/api/v1/review-cases/*/commands", async (route) => {
    captured = { body: route.request().postDataJSON() as Record<string, unknown>, csrf: route.request().headers()["x-csrf-token"] ?? "" };
    await route.abort();
  });
  await page.getByRole("button", { name: "Claim this case" }).click();
  await expect.poll(() => captured !== null).toBe(true);
  await page.unroute("**/api/v1/review-cases/*/commands");
  return captured!;
}

async function post(request: APIRequestContext, captured: { body: Record<string, unknown>; csrf: string }, options: { headers?: Record<string, string>; body?: unknown; url?: string; raw?: string } = {}) {
  return request.post(options.url ?? COMMAND_URL(URLS.commit), {
    headers: { origin: URLS.commit, "x-csrf-token": captured.csrf, ...(options.headers ?? {}) },
    ...(options.raw !== undefined ? { data: options.raw } : { data: options.body ?? captured.body }),
  });
}

test.describe("server-side POST boundary", () => {
  test("the genuine UI request is accepted (control case)", async ({ page, request }) => {
    const captured = await captureUiRequest(page);
    const response = await post(request, captured);
    expect(response.status()).toBe(200);
    expect((await response.json()).status).toBe("ACCEPTED");
  });

  test("GET and every non-POST method on the command path are unavailable", async ({ request }) => {
    for (const method of ["get", "put", "patch", "delete", "head"] as const) {
      const response = await request[method](COMMAND_URL(URLS.commit));
      expect([404, 405], method).toContain(response.status());
    }
    expect((await stats(request)).commandPosts).toBe(0);
  });

  test("the read-only proxy still refuses command paths", async ({ request }) => {
    for (const path of [`/api/backend/api/v1/review-cases/${caseId(1)}/commands`, `/api/backend/api/v1/review-cases/${caseId(1)}/command-capabilities`]) {
      const response = await request.get(`${URLS.commit}${path}`);
      expect(response.status()).toBe(404);
      const post = await request.post(`${URLS.commit}${path}`, { data: {} });
      expect([404, 405]).toContain(post.status());
    }
  });

  test("only the exact command path exists: sub-paths, traversal and junk ids are rejected", async ({ page, request }) => {
    const captured = await captureUiRequest(page);
    const id = caseId(CLAIM_CASE);
    const attempts = [
      `${URLS.commit}/api/v1/review-cases/${id}/commands/extra`,
      `${URLS.commit}/api/v1/review-cases/%252e%252e/commands`,
      `${URLS.commit}/api/v1/review-cases/${id}%2f..%2fcommands`,
      `${URLS.commit}/api/v1/review-cases/${id}/commands%2f..%2f..%2f`,
      `${URLS.commit}/api/v1/review-cases/..%2f${id}/commands`,
      `${URLS.commit}/api/v1/review-cases/not-a-uuid/commands`,
      `${URLS.commit}/api/v1/review-cases//commands`,
      `${URLS.commit}/api/v1/review-cases/${id}/commands%00`,
      `${URLS.commit}/api/v1/review-cases/${id}`,
      `${URLS.commit}/api/v1/review-cases`,
      `${URLS.commit}/api/v1/dashboard`,
    ];
    for (const url of attempts) {
      const response = await post(request, captured, { url });
      expect([400, 404, 405, 308], url).toContain(response.status());
    }
    expect((await stats(request)).commandPosts).toBe(0);
  });

  test("cross-origin and forged-metadata submissions are rejected before reaching the backend", async ({ page, request }) => {
    const captured = await captureUiRequest(page);
    const forgedHeaders: Record<string, string>[] = [
      { origin: "https://evil.example" },
      { origin: "http://127.0.0.1:1" },
      { "sec-fetch-site": "cross-site" },
      { "sec-fetch-site": "same-site" },
      { origin: "null" },
    ];
    for (const headers of forgedHeaders) {
      const response = await post(request, captured, { headers });
      expect(response.status(), JSON.stringify(headers)).toBe(403);
      expect((await response.json()).errors).toEqual(["CSRF_ORIGIN_REJECTED"]);
    }
    const noOrigin = await request.post(COMMAND_URL(URLS.commit), { headers: { "x-csrf-token": captured.csrf }, data: captured.body });
    expect(noOrigin.status()).toBe(403);
    expect((await stats(request)).commandPosts).toBe(0);
  });

  test("CSRF token: missing, forged, and one bound to a different case are rejected", async ({ page, request }) => {
    const captured = await captureUiRequest(page);
    for (const token of ["", "forged", "9999999999.AAAA", captured.csrf + "x"]) {
      const response = await post(request, captured, { headers: { "x-csrf-token": token } });
      expect(response.status(), token).toBe(403);
      expect((await response.json()).errors).toEqual(["CSRF_TOKEN_INVALID"]);
    }
    const otherCase = await post(request, captured, { url: COMMAND_URL(URLS.commit, caseId(2)) });
    expect(otherCase.status()).toBe(403);
    expect((await stats(request)).commandPosts).toBe(0);
  });

  test("content type, size and JSON hygiene", async ({ page, request }) => {
    const captured = await captureUiRequest(page);

    const wrongType = await request.post(COMMAND_URL(URLS.commit), {
      headers: { origin: URLS.commit, "x-csrf-token": captured.csrf, "content-type": "text/plain" }, data: JSON.stringify(captured.body),
    });
    expect(wrongType.status()).toBe(415);

    const form = await request.post(COMMAND_URL(URLS.commit), {
      headers: { origin: URLS.commit, "x-csrf-token": captured.csrf }, form: { action: "CLAIM" },
    });
    expect(form.status()).toBe(415);

    const oversized = await post(request, captured, { body: { ...captured.body, notes: "x".repeat(200_000) } });
    expect(oversized.status()).toBe(413);

    const malformed = await request.post(COMMAND_URL(URLS.commit), {
      headers: { origin: URLS.commit, "x-csrf-token": captured.csrf, "content-type": "application/json" }, data: Buffer.from("{not json"),
    });
    expect(malformed.status()).toBe(400);
    expect((await malformed.json()).errors).toEqual(["MALFORMED_JSON"]);
    expect(JSON.stringify(await malformed.json())).not.toMatch(/SyntaxError|Unexpected|position/);

    expect((await stats(request)).commandPosts).toBe(0);
  });

  test("identity, mode and unknown fields cannot be supplied by the browser", async ({ page, request }) => {
    const captured = await captureUiRequest(page);
    for (const extra of [
      { tenant_id: "99999999-9999-4999-8999-999999999999" }, { actor_id: "attacker" }, { actor_role: "TENANT_ADMIN" }, { role: "TENANT_ADMIN" },
      { requested_at: "2001-01-01T00:00:00Z" }, { command_mode: "commit" }, { payment_amount: "1.00" }, { bank_account: "GB00" }, { erp_posting: true },
    ]) {
      const response = await post(request, captured, { body: { ...captured.body, ...extra } });
      expect(response.status(), JSON.stringify(extra)).toBe(422);
    }
    expect((await stats(request)).commandPosts).toBe(0);
  });

  test("unsupported and payment actions are rejected and never forwarded", async ({ page, request }) => {
    const captured = await captureUiRequest(page);
    for (const action of ["CONFIRM_SUPPLIER", "CONFIRM_PURCHASE_ORDER", "REQUEST_INFORMATION", "ESCALATE", "EXECUTE_PAYMENT", "RELEASE_PAYMENT", "BANK_TRANSFER", "POST_TO_ERP"]) {
      const response = await post(request, captured, { body: { ...captured.body, action } });
      expect(response.status(), action).toBe(422);
    }
    expect((await stats(request)).commandPosts).toBe(0);
  });

  test("authentication headers are built server-side; browser-supplied ones never reach the backend", async ({ page, request }) => {
    const captured = await captureUiRequest(page);
    const response = await post(request, captured, {
      headers: {
        "x-tenant-id": "99999999-9999-4999-8999-999999999999", "x-actor-id": "attacker", "x-actor-role": "TENANT_ADMIN",
        "x-authenticated-at": "2001-01-01T00:00:00Z", authorization: "Bearer stolen-token", cookie: "session=stolen", "x-request-id": "browser-supplied",
      },
    });
    expect(response.status()).toBe(200);

    const last = (await stats(request)).lastCommand!;
    expect(last.headers.tenant).toBe("00000000-0000-0000-0000-000000000000");
    expect(last.headers.actor).toBe("reviewer-1");
    expect(last.headers.role).toBe("AP_REVIEWER");
    expect(last.headers.authorization).toBeNull();
    expect(last.headers.cookie).toBeNull();
    expect(new Date(String(last.headers.authenticatedAt)).getFullYear()).toBeGreaterThan(2024);
    const body = last.body!;
    expect(new Date(String(body.requested_at)).getTime()).toBeGreaterThanOrEqual(new Date(String(last.headers.authenticatedAt)).getTime());
    expect(new Date(String(body.requested_at)).getFullYear()).toBeGreaterThan(2024);
  });

  test("a stale conflict, upstream junk and outages are redacted", async ({ page, request }) => {
    const captured = await captureUiRequest(page);
    const stale = await post(request, captured, { body: { ...captured.body, observed_workflow_revision: 42 } });
    expect(stale.status()).toBe(409);
    expect((await stale.json()).errors).toContain("STALE_WORKFLOW_REVISION");

    await control(request, { mode: "malformed" });
    const malformed = await post(request, captured);
    expect(malformed.status()).toBe(502);
    expect(JSON.stringify(await malformed.json())).not.toMatch(/Unexpected|position|token|127\.0\.0\.1/);

    await control(request, { mode: "unavailable" });
    const down = await post(request, captured);
    expect(down.status()).toBe(503);
    expect(JSON.stringify(await down.json())).not.toMatch(/ECONNREFUSED|127\.0\.0\.1|localhost|fetch failed/i);
  });

  test("mode mismatch is refused server-side, not just hidden in the UI", async ({ page, request }) => {
    const captured = await captureUiRequest(page);
    await control(request, { health_mode: "VALIDATION_ONLY" });
    const response = await post(request, captured);
    expect(response.status()).toBe(409);
    expect((await response.json()).errors).toEqual(["COMMAND_MODE_MISMATCH"]);
    expect((await stats(request)).commandPosts).toBe(0);
  });

  test("the auditor's server cannot be used to mutate: the backend refuses the role", async ({ page, request }) => {
    await page.goto(`${URLS.auditor}/review-cases/${caseId(CLAIM_CASE)}`);
    // No controls exist, but a hand-forged request with a valid page token is still refused by the backend.
    const html = await page.content();
    const token = /csrfToken\\?":\\?"([0-9]+\.[A-Za-z0-9_-]+)/.exec(html)?.[1];
    expect(token).toBeTruthy();
    const response = await request.post(COMMAND_URL(URLS.auditor), {
      headers: { origin: URLS.auditor, "x-csrf-token": token! },
      data: { command_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", idempotency_key: "auditor-forged-0001", action: "CLAIM", disposition: null, observed_review_revision: 1, observed_workflow_revision: 1, reason_codes: [], notes: null, corrections: [] },
    });
    expect(response.status()).toBe(403);
    expect((await response.json()).errors).toContain("ACTION_NOT_PERMITTED");
    await expect(page.getByTestId("review-action-workspace")).toBeVisible();
  });
});

test.describe("secret exposure (task §22)", () => {
  const FORBIDDEN = [
    /postgres(?:ql)?:\/\/[^\s"'<>]+:[^\s"'<>]+@/i, /AP_AGENT_TEST_POSTGRES_DSN/, /AP_AGENT_POSTGRES_DSN/, /neon\.tech/i,
    /AP_AGENT_DEV_TENANT_ID/, /AP_AGENT_DEV_ACTOR_ID/, /AP_AGENT_DEV_ACTOR_ROLE/, /AP_AGENT_FRONTEND_REVIEW_COMMAND_MODE/,
    /AP_AGENT_FRONTEND_CSRF_SECRET/, /00000000-0000-0000-0000-000000000000/, /READ_ONLY_AUDITOR/,
  ];
  // The configured actor id is never in an asset. (After an action it legitimately appears as *data*, in
  // the timeline's "claimed by" row, exactly as decision/assignee ids already do -- so it is only asserted
  // absent from static JavaScript and from pages loaded before any action.)
  const ACTOR_ID = /reviewer-1/;

  test("rendered HTML, JavaScript, storage, cookies and console carry no secrets or identity", async ({ page, request }) => {
    const problems = watchConsole(page);
    const scripts: string[] = [];
    page.on("response", async (response) => {
      if (response.url().includes("/_next/static/") && response.url().endsWith(".js")) scripts.push(await response.text());
    });

    await openCase(page, URLS.commit, CLAIM_CASE);
    expect(await page.content()).not.toMatch(ACTOR_ID);
    await page.getByRole("button", { name: "Claim this case" }).click();
    await expect(page.getByTestId("command-result")).toHaveAttribute("data-result", "executed");

    const html = await page.content();
    for (const pattern of FORBIDDEN) expect(html, String(pattern)).not.toMatch(pattern);
    expect(scripts.length).toBeGreaterThan(0);
    for (const script of scripts) for (const pattern of [...FORBIDDEN, ACTOR_ID]) expect(script, String(pattern)).not.toMatch(pattern);

    const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, cookie: document.cookie }));
    expect(storage).toBe('{"local":{},"session":{},"cookie":""}');
    expect((await page.context().cookies()).length).toBe(0);
    expect(JSON.stringify(problems)).not.toMatch(/postgres|tenant|token/i);
    expect(problems).toEqual([]);

    const response = await request.get(`${URLS.commit}/review-cases/${caseId(CLAIM_CASE)}`);
    const body = await response.text();
    for (const pattern of [...FORBIDDEN]) expect(body, String(pattern)).not.toMatch(pattern);
    await request.get(`${MOCK}/__stats`);
  });

  test("the auditor page exposes no actor identity or command mode either", async ({ page }) => {
    await page.goto(`${URLS.auditor}/review-cases/${caseId(CLAIM_CASE)}`);
    const html = await page.content();
    for (const pattern of [...FORBIDDEN, ACTOR_ID]) expect(html, String(pattern)).not.toMatch(pattern);
  });
});

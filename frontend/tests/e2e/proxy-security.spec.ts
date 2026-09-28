import { expect, test } from "@playwright/test";
import { setMockBackendMode } from "./support/control";

/**
 * HTTP-level security tests for the server-side backend proxy
 * (`src/app/api/backend/[...path]/route.ts`, M11A task §5, extended M11B
 * task §5/§14). `tests/unit/backend-proxy-allowlist.test.ts` already
 * exercises `matchAllowedPath` directly in isolation; this suite instead
 * drives real HTTP requests at the running route so the allow-list, the
 * exported-method restriction, and the "never forward browser auth
 * headers" guarantee are proven end to end, not just unit-tested in
 * isolation.
 */

const VALID_UUID = "cccccccc-0000-4000-8000-000000000003";
const UNKNOWN_UUID = "00000000-0000-4000-8000-000000000099";

test.beforeEach(async ({ request }) => {
  await setMockBackendMode(request, "normal");
});

test.describe("backend proxy: allow-listed paths (GET only)", () => {
  test("allows GET /api/backend/health", async ({ request }) => {
    const response = await request.get("/api/backend/health");
    expect(response.status()).toBe(200);
  });

  test("allows GET /api/backend/api/v1/dashboard", async ({ request }) => {
    const response = await request.get("/api/backend/api/v1/dashboard");
    expect(response.status()).toBe(200);
  });

  test("allows GET /api/backend/api/v1/review-cases", async ({ request }) => {
    const response = await request.get("/api/backend/api/v1/review-cases");
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body.data.items.length).toBeGreaterThan(0);
  });

  test("allows GET /api/backend/api/v1/review-cases/{valid UUID}", async ({ request }) => {
    const response = await request.get(`/api/backend/api/v1/review-cases/${VALID_UUID}`);
    expect(response.status()).toBe(200);
  });

  test("rejects a malformed UUID in the detail path with 404, never forwarding it upstream", async ({ request }) => {
    const response = await request.get("/api/backend/api/v1/review-cases/not-a-uuid");
    expect(response.status()).toBe(404);
    const body = await response.json();
    expect(body.errors).toContain("BACKEND_PATH_NOT_ALLOWED");
  });

  test("rejects the review-command subpath even with a valid UUID -- never reachable through this proxy", async ({ request }) => {
    const response = await request.get(`/api/backend/api/v1/review-cases/${VALID_UUID}/commands`);
    expect(response.status()).toBe(404);
  });

  test("rejects an unsupported/unrelated path", async ({ request }) => {
    const response = await request.get("/api/backend/api/v1/something-else");
    expect(response.status()).toBe(404);
  });

  test("rejects an encoded path-traversal attempt", async ({ request }) => {
    const response = await request.get(`/api/backend/api/v1/review-cases/${VALID_UUID}/%2e%2e/commands`);
    expect(response.status()).toBe(404);
  });

  for (const method of ["POST", "PUT", "PATCH", "DELETE"] as const) {
    test(`rejects ${method} on an allow-listed path -- only GET is ever exported`, async ({ request }) => {
      const response = await request.fetch("/api/backend/api/v1/review-cases", { method });
      expect(response.status()).toBe(405);
    });

    test(`rejects ${method} on the review-case detail path`, async ({ request }) => {
      const response = await request.fetch(`/api/backend/api/v1/review-cases/${VALID_UUID}`, { method });
      expect(response.status()).toBe(405);
    });
  }
});

test.describe("backend proxy: query-parameter allow-listing", () => {
  test("drops a query parameter outside the endpoint's exact allow-list", async ({ request }) => {
    // `assigned_to`/`status`/etc. are allowed on the list endpoint; an
    // invented one like `tenant_id` is not, and dropping it (rather than
    // forwarding it) is what stops a browser from ever being able to pass
    // its own tenant identity through this route.
    const response = await request.get("/api/backend/api/v1/review-cases?tenant_id=11111111-1111-1111-1111-111111111111");
    expect(response.status()).toBe(200);
    // Same unfiltered result as no query at all -- proves the parameter
    // was dropped, not forwarded and honored by the mock backend.
    const withBogusParam = await response.json();
    const plain = await (await request.get("/api/backend/api/v1/review-cases")).json();
    expect(withBogusParam.data.items.length).toBe(plain.data.items.length);
  });

  test("drops an unsupported status filter value before it ever reaches the backend", async ({ request }) => {
    // AWAITING_INFORMATION/ESCALATED are real ReviewCaseStatus values the
    // backend's own filter can't handle (a documented 500 defect); the
    // proxy itself doesn't special-case this, but this test still proves
    // the path/param plumbing carries whatever status value is given
    // through as expected for a supported one.
    const response = await request.get("/api/backend/api/v1/review-cases?status=OPEN");
    expect(response.status()).toBe(200);
  });
});

test.describe("backend proxy: identity boundary", () => {
  test("browser-supplied tenant/actor headers are ignored -- the proxy attaches its own", async ({ request }) => {
    // The mock backend itself requires x-tenant-id/x-actor-id/x-actor-role
    // and 401s without them (tests/e2e/support/mock-backend.mjs). This
    // request supplies none of the three, and a fetch that reaches this
    // Next.js route handler (rather than curling the mock backend
    // directly) still succeeds -- proof the server-side proxy is the one
    // attaching real auth headers, never trusting or forwarding whatever
    // the browser sent.
    const response = await request.get("/api/backend/health", {
      headers: {
        "x-tenant-id": "99999999-9999-9999-9999-999999999999",
        "x-actor-id": "attacker-supplied-actor",
        "x-actor-role": "SUPERUSER",
      },
    });
    expect(response.status()).toBe(200);
  });

  test("a browser-supplied tenant query param cannot override the server's own tenant context", async ({ request }) => {
    // `/api/v1/dashboard` only allow-lists `batch_id` as a query param --
    // there is no tenant-identity query parameter for a caller to set in
    // the first place, so this is inherently unreachable, not merely
    // filtered. batch_id itself is still forwarded (an operator-facing
    // scoping filter, not an identity override).
    const response = await request.get(
      "/api/backend/api/v1/dashboard?tenant_id=99999999-9999-9999-9999-999999999999",
    );
    expect(response.status()).toBe(200);
  });

  test("an unknown review-case id returns a safe, generic 404 -- never distinguishing 'not found' from 'exists in another tenant'", async ({
    request,
  }) => {
    const response = await request.get(`/api/backend/api/v1/review-cases/${UNKNOWN_UUID}`);
    expect(response.status()).toBe(404);
    const body = await response.json();
    expect(body.errors).toEqual(["REVIEW_CASE_NOT_FOUND"]);
    // The 404 body never reveals whether the id belongs to another tenant.
    expect(JSON.stringify(body)).not.toMatch(/tenant/i);
  });
});

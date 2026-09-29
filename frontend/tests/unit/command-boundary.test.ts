// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import { handleCommandRequest, type BoundaryDeps } from "@/lib/server/command-boundary";
import { ServerConfigError, type ServerEnvConfig } from "@/lib/config/server-env";
import { MAX_REQUEST_BODY_BYTES } from "@/lib/commands/contract";
import { CASE_ID, COMMAND_ID, executedEnvelope, validatedEnvelope } from "../support/command-fixtures";

const TENANT = "abcdef01-2345-4678-89ab-cdef01234567";
const ENV: ServerEnvConfig = {
  apiBaseUrl: "http://backend.internal:8000", authMode: "development_headers",
  devTenantId: TENANT, devActorId: "reviewer-x", devActorRole: "AP_REVIEWER",
};
const FIXED_NOW = new Date("2026-03-01T12:00:00.000Z");

const validBody = () => ({
  command_id: COMMAND_ID, idempotency_key: "ap-ui-" + COMMAND_ID, action: "CLAIM", disposition: null,
  observed_review_revision: 1, observed_workflow_revision: 1, reason_codes: [], notes: null, corrections: [],
});

type Call = { url: string; init: RequestInit };

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function harness(overrides: Partial<BoundaryDeps> = {}, upstream: (call: Call) => Response | Promise<Response> = () => json(executedEnvelope())) {
  const calls: Call[] = [];
  let tick = 0;
  const deps: BoundaryDeps = {
    loadMode: () => ({ mode: "commit", valid: true }),
    loadEnv: () => ENV,
    fetchImpl: (async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init: init ?? {} });
      if (url.endsWith("/health")) return json({ request_id: "x", status: "SUCCEEDED", data: { command_mode: "COMMIT" }, errors: [], generated_at: "x" });
      return upstream({ url, init: init ?? {} });
    }) as typeof fetch,
    now: () => new Date(FIXED_NOW.getTime() + tick++),
    verifyCsrf: (token) => token === "good-token",
    timeoutMs: 500,
    newRequestId: () => "77777777-7777-4777-8777-777777777777",
    ...overrides,
  };
  return { deps, calls };
}

function post(body: unknown, headers: Record<string, string> = {}, raw?: string) {
  return new Request("http://127.0.0.1:3000/api/v1/review-cases/x/commands", {
    method: "POST",
    headers: {
      "content-type": "application/json", origin: "http://127.0.0.1:3000", host: "127.0.0.1:3000",
      "sec-fetch-site": "same-origin", "x-csrf-token": "good-token", ...headers,
    },
    body: raw ?? JSON.stringify(body),
  });
}

const upstreamCommandCalls = (calls: Call[]) => calls.filter((call) => call.url.includes("/commands"));

describe("command boundary: gating (task §3/§7)", () => {
  it("forwards nothing when the frontend mode is disabled, regardless of the backend mode", async () => {
    const { deps, calls } = harness({ loadMode: () => ({ mode: "disabled", valid: true }) });
    const result = await handleCommandRequest(post(validBody()), CASE_ID, deps);
    expect(result.status).toBe(403);
    expect(result.body).toMatchObject({ errors: ["REVIEW_COMMANDS_DISABLED"] });
    expect(calls).toHaveLength(0);
  });

  it("forwards nothing when the mode setting is invalid", async () => {
    const { deps, calls } = harness({ loadMode: () => ({ mode: "disabled", valid: false }) });
    const result = await handleCommandRequest(post(validBody()), CASE_ID, deps);
    expect(result.status).toBe(403);
    expect(result.body).toMatchObject({ errors: ["COMMAND_MODE_MISCONFIGURED"] });
    expect(calls).toHaveLength(0);
  });

  it.each([
    ["frontend validation_only + backend COMMIT", "validation_only", "COMMIT"],
    ["frontend commit + backend VALIDATION_ONLY", "commit", "VALIDATION_ONLY"],
  ] as const)("fails closed on mode mismatch: %s", async (_label, frontend, backend) => {
    const { deps, calls } = harness({
      loadMode: () => ({ mode: frontend, valid: true }),
      fetchImpl: (async (input: string | URL | Request, init?: RequestInit) => {
        calls.push({ url: String(input), init: init ?? {} });
        return json({ status: "SUCCEEDED", data: { command_mode: backend }, errors: [], request_id: "x", generated_at: "x" });
      }) as typeof fetch,
    });
    const result = await handleCommandRequest(post(validBody()), CASE_ID, deps);
    expect(result.status).toBe(409);
    expect(result.body).toMatchObject({ errors: ["COMMAND_MODE_MISMATCH"] });
    expect(upstreamCommandCalls(calls)).toHaveLength(0);
    expect(JSON.stringify(result.body)).not.toMatch(/COMMIT|VALIDATION_ONLY|backend\.internal/);
  });

  it("fails closed when the backend mode cannot be read", async () => {
    for (const health of [() => new Response("not json", { status: 200 }), () => json({ data: {} }), () => json({ data: { command_mode: 7 } })]) {
      const calls: Call[] = [];
      const { deps } = harness({
        fetchImpl: (async (input: string | URL | Request, init?: RequestInit) => {
          calls.push({ url: String(input), init: init ?? {} });
          return health();
        }) as typeof fetch,
      });
      const result = await handleCommandRequest(post(validBody()), CASE_ID, deps);
      expect(result.status).toBe(409);
      expect(upstreamCommandCalls(calls)).toHaveLength(0);
    }
  });

  it("reports an unreachable backend as 503 without exposing the host", async () => {
    const { deps } = harness({ fetchImpl: (async () => { throw new Error("connect ECONNREFUSED backend.internal:8000"); }) as typeof fetch });
    const result = await handleCommandRequest(post(validBody()), CASE_ID, deps);
    expect(result.status).toBe(503);
    expect(result.body).toMatchObject({ errors: ["BACKEND_UNAVAILABLE"] });
    expect(JSON.stringify(result.body)).not.toContain("backend.internal");
  });

  it("maps an aborted upstream call to a timeout", async () => {
    const { deps } = harness({ fetchImpl: (async () => { throw new DOMException("aborted", "AbortError"); }) as typeof fetch });
    const result = await handleCommandRequest(post(validBody()), CASE_ID, deps);
    expect(result.body).toMatchObject({ errors: ["BACKEND_TIMEOUT"] });
  });
});

describe("command boundary: CSRF, content type, size, parsing (task §7/§22)", () => {
  it.each([
    ["cross-origin", { origin: "https://evil.example" }],
    ["cross-site fetch metadata", { "sec-fetch-site": "cross-site" }],
  ])("rejects a %s submission", async (_label, headers) => {
    const { deps, calls } = harness();
    const result = await handleCommandRequest(post(validBody(), headers), CASE_ID, deps);
    expect(result.status).toBe(403);
    expect(result.body).toMatchObject({ errors: ["CSRF_ORIGIN_REJECTED"] });
    expect(calls).toHaveLength(0);
  });

  it("rejects a missing or wrong CSRF token", async () => {
    for (const headers of [{ "x-csrf-token": "" }, { "x-csrf-token": "forged" }]) {
      const { deps, calls } = harness();
      const result = await handleCommandRequest(post(validBody(), headers), CASE_ID, deps);
      expect(result.status).toBe(403);
      expect(result.body).toMatchObject({ errors: ["CSRF_TOKEN_INVALID"] });
      expect(calls).toHaveLength(0);
    }
  });

  it("rejects a non-JSON content type", async () => {
    const { deps, calls } = harness();
    const result = await handleCommandRequest(post(validBody(), { "content-type": "text/plain" }), CASE_ID, deps);
    expect(result.status).toBe(415);
    expect(calls).toHaveLength(0);
  });

  it("accepts JSON with a charset parameter", async () => {
    const { deps } = harness();
    const result = await handleCommandRequest(post(validBody(), { "content-type": "application/json; charset=utf-8" }), CASE_ID, deps);
    expect(result.status).toBe(200);
  });

  it("rejects an oversized body by declared length and by streamed size", async () => {
    const { deps, calls } = harness();
    const declared = await handleCommandRequest(post(validBody(), { "content-length": String(MAX_REQUEST_BODY_BYTES + 1) }), CASE_ID, deps);
    expect(declared.status).toBe(413);
    const streamed = await handleCommandRequest(post(null, {}, JSON.stringify({ ...validBody(), notes: "x".repeat(MAX_REQUEST_BODY_BYTES + 10) })), CASE_ID, deps);
    expect(streamed.status).toBe(413);
    expect(calls).toHaveLength(0);
  });

  it("rejects malformed JSON safely", async () => {
    const { deps, calls } = harness();
    const result = await handleCommandRequest(post(null, {}, "{not json"), CASE_ID, deps);
    expect(result.status).toBe(400);
    expect(result.body).toMatchObject({ errors: ["MALFORMED_JSON"] });
    expect(calls).toHaveLength(0);
  });

  it.each(["../../etc/passwd", "%2e%2e", "not-a-uuid", "", CASE_ID + "/extra", CASE_ID.slice(0, 35)])("rejects the review-case id %j", async (id) => {
    const { deps, calls } = harness();
    const result = await handleCommandRequest(post(validBody()), id, deps);
    expect(result.status).toBe(404);
    expect(calls).toHaveLength(0);
  });

  it.each([
    ["tenant_id", TENANT], ["actor_id", "spoofed"], ["actor_role", "TENANT_ADMIN"], ["requested_at", "2020-01-01T00:00:00Z"],
    ["command_mode", "commit"], ["payment_amount", "1.00"],
  ])("rejects a browser-supplied %s", async (field, value) => {
    const { deps, calls } = harness();
    const result = await handleCommandRequest(post({ ...validBody(), [field]: value }), CASE_ID, deps);
    expect(result.status).toBe(422);
    expect(calls).toHaveLength(0);
  });

  it.each(["EXECUTE_PAYMENT", "RELEASE_PAYMENT", "BANK_TRANSFER", "POST_TO_ERP", "ESCALATE", "REQUEST_INFORMATION"])("never forwards the action %s", async (action) => {
    const { deps, calls } = harness();
    const result = await handleCommandRequest(post({ ...validBody(), action }), CASE_ID, deps);
    expect(result.status).toBe(422);
    expect(JSON.stringify(result.body)).toContain("REVIEW_ACTION_NOT_SUPPORTED");
    expect(calls).toHaveLength(0);
  });

  it("reports a missing server configuration without naming variables or values", async () => {
    const { deps } = harness({ loadEnv: () => { throw new ServerConfigError("MISSING_VARIABLE", "AP_AGENT_API_BASE_URL", "secret detail"); } });
    const result = await handleCommandRequest(post(validBody()), CASE_ID, deps);
    expect(result.status).toBe(500);
    expect(JSON.stringify(result.body)).not.toMatch(/AP_AGENT|secret detail/);
  });
});

describe("command boundary: upstream request construction (task §7)", () => {
  it("builds authentication headers and requested_at server-side, ignoring anything browser-supplied", async () => {
    const { deps, calls } = harness();
    const request = post(validBody(), { "x-tenant-id": "evil", "x-actor-id": "evil", "x-actor-role": "TENANT_ADMIN", "x-authenticated-at": "1999-01-01T00:00:00Z", authorization: "Bearer stolen", cookie: "a=b", "x-request-id": "browser-corr" });
    const result = await handleCommandRequest(request, CASE_ID, deps);
    expect(result.status).toBe(200);

    const [call] = upstreamCommandCalls(calls);
    const headers = call!.init.headers as Record<string, string>;
    expect(headers["X-Tenant-ID"]).toBe(TENANT);
    expect(headers["X-Actor-ID"]).toBe("reviewer-x");
    expect(headers["X-Actor-Role"]).toBe("AP_REVIEWER");
    expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toEqual(expect.arrayContaining(["authorization", "cookie", "x-request-id"]));

    const body = JSON.parse(String(call!.init.body)) as Record<string, unknown>;
    expect(new Date(String(headers["X-Authenticated-At"])).getTime()).toBeLessThanOrEqual(new Date(String(body.requested_at)).getTime());
    expect(String(body.requested_at).startsWith("2026-03-01T12:00:00")).toBe(true);
    expect(Object.keys(body).sort()).toEqual([
      "action", "command_id", "corrections", "disposition", "idempotency_key", "notes",
      "observed_review_revision", "observed_workflow_revision", "reason_codes", "requested_at",
    ]);
    expect(String(body)).not.toContain("evil");
  });

  it("forwards only the exact command URL for the validated case id", async () => {
    const { deps, calls } = harness();
    await handleCommandRequest(post(validBody()), CASE_ID.toUpperCase(), deps);
    const [call] = upstreamCommandCalls(calls);
    expect(call!.url).toBe(`http://backend.internal:8000/api/v1/review-cases/${CASE_ID}/commands`);
    expect(call!.init.method).toBe("POST");
  });

  it("preserves exact corrections (decimal strings, evidence ids) on the wire", async () => {
    const { deps, calls } = harness();
    const body = {
      ...validBody(), action: "CORRECT", disposition: "CORRECTED", reason_codes: ["EVIDENCE_VERIFIED"],
      corrections: [{ field_name: "TOTAL_AMOUNT", line_number: null, previous_value: "100.10", corrected_value: "100.100", reason: "Checked", evidence_reference_ids: ["ev-total-1"] }],
    };
    await handleCommandRequest(post(body), CASE_ID, deps);
    const sent = JSON.parse(String(upstreamCommandCalls(calls)[0]!.init.body)) as { corrections: unknown[] };
    expect(sent.corrections).toEqual(body.corrections);
  });
});

describe("command boundary: response mapping (task §18)", () => {
  it("returns validation-only, committed and idempotent results unchanged in shape", async () => {
    for (const envelope of [validatedEnvelope(), executedEnvelope(), { ...executedEnvelope(), status: "IDEMPOTENT" }]) {
      const { deps } = harness({}, () => json(envelope));
      const result = await handleCommandRequest(post(validBody()), CASE_ID, deps);
      expect(result.status).toBe(200);
      expect(result.body).toEqual(envelope);
    }
  });

  it.each([401, 403, 404, 409, 422, 500, 503])("passes %i through with only well-formed codes", async (status) => {
    const { deps } = harness({}, () =>
      json({ request_id: "12345678-1234-4234-8234-123456789012", errors: ["STALE_REVIEW_REVISION", "select * from secret where dsn='postgres://u:p@h/db'"], generated_at: "x" }, status),
    );
    const result = await handleCommandRequest(post(validBody()), CASE_ID, deps);
    expect(result.status).toBe(status);
    expect(result.body).toMatchObject({ request_id: "12345678-1234-4234-8234-123456789012", errors: ["STALE_REVIEW_REVISION"] });
    expect(JSON.stringify(result.body)).not.toMatch(/postgres|select \*/i);
  });

  it("substitutes a controlled code when the upstream error body is empty or junk", async () => {
    for (const [status, code] of [[409, "STALE_REVIEW_REVISION"], [500, "INTERNAL_ERROR"], [503, "DATABASE_UNAVAILABLE"]] as const) {
      const { deps } = harness({}, () => json({ detail: "Traceback (most recent call last): File \"/home/user/app.py\"" }, status));
      const result = await handleCommandRequest(post(validBody()), CASE_ID, deps);
      expect(result.body).toMatchObject({ errors: [code] });
      expect(JSON.stringify(result.body)).not.toMatch(/Traceback|\/home\//);
    }
  });

  it("maps unexpected statuses to a redacted 502", async () => {
    const { deps } = harness({}, () => json({ errors: ["X_1"] }, 418));
    expect((await handleCommandRequest(post(validBody()), CASE_ID, deps)).status).toBe(502);
  });

  it("treats a non-JSON or malformed 200 as MALFORMED_RESPONSE, never as success", async () => {
    for (const upstream of [() => new Response("<html>oops</html>", { status: 200 }), () => json({ status: "ACCEPTED", data: { junk: true } })]) {
      const { deps } = harness({}, upstream);
      const result = await handleCommandRequest(post(validBody()), CASE_ID, deps);
      expect(result.status).toBe(502);
      expect(result.body).toMatchObject({ errors: ["MALFORMED_RESPONSE"] });
    }
  });

  it("does not call the backend command endpoint more than once per request", async () => {
    const spy = vi.fn(() => json(executedEnvelope()));
    const { deps, calls } = harness({}, spy);
    await handleCommandRequest(post(validBody()), CASE_ID, deps);
    expect(upstreamCommandCalls(calls)).toHaveLength(1);
    expect(spy).toHaveBeenCalledTimes(1);
  });
});

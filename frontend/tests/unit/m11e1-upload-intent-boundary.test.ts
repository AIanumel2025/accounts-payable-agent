// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { handleCreateUploadIntent, handleFinalizeUploadIntent } from "@/lib/server/upload-intent-boundary";
import type { UploadDeps } from "@/lib/server/upload-boundary";
import type { ServerEnvConfig } from "@/lib/config/server-env";

const ENV: ServerEnvConfig = { apiBaseUrl: "https://api.example.on.aws", authMode: "clerk_jwt", platformAuthMode: "aws_sigv4" };
const INTENT = "5b6f3c1e-8f0a-4c2d-9a11-0c4f0f7a2b11";
const JOB = "77777777-7777-4777-8777-777777777777";
const SHA = "a".repeat(64);
const BODY = { filename: "Invoice 1.pdf", media_type: "application/pdf", byte_size: 1234, sha256: SHA };

type Call = { url: string; init: RequestInit };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const intentResponse = (over: Record<string, unknown> = {}) =>
  json({ request_id: "x", status: "CREATED", errors: [], generated_at: "x", data: { intent_id: INTENT, upload_url: "https://bucket.s3.eu-west-2.amazonaws.com/", upload_fields: { key: "k" }, maximum_bytes: 10485760, ...over } }, 201);
const accepted = (status = 202) => json({ request_id: "x", status: "ACCEPTED", errors: [], generated_at: "x", data: { idempotent_replay: false, job: { job_id: JOB, status: "QUEUED" } } }, status);

function harness(upstream: (call: Call) => Response, overrides: Partial<UploadDeps> = {}) {
  const calls: Call[] = [];
  const deps: UploadDeps = {
    loadMode: () => ({ mode: "enabled", valid: true }),
    loadEnv: () => ENV,
    fetchImpl: (async (input: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(input), init: init ?? {} });
      return upstream({ url: String(input), init: init ?? {} });
    }) as typeof fetch,
    now: () => new Date("2026-03-01T12:00:00.000Z"),
    verifyCsrf: (token) => token === "good-token",
    authHeaders: async () => ({ ok: true, headers: { Authorization: "Bearer clerk-session" } }),
    timeoutMs: 500,
    newRequestId: () => "88888888-8888-4888-8888-888888888888",
    ...overrides,
  };
  return { deps, calls };
}

const headers = (extra: Record<string, string> = {}) => ({
  origin: "https://app.example.on.aws", host: "app.example.on.aws", "sec-fetch-site": "same-origin", "x-csrf-token": "good-token", ...extra,
});
const createRequest = (body: unknown = BODY, extra: Record<string, string> = {}) =>
  new Request("https://app.example.on.aws/api/v1/operations/upload-intents", {
    method: "POST", headers: { "content-type": "application/json", ...headers(extra) }, body: typeof body === "string" ? body : JSON.stringify(body),
  });
const finalizeRequest = (extra: Record<string, string> = {}) =>
  new Request(`https://app.example.on.aws/api/v1/operations/upload-intents/${INTENT}/finalize`, { method: "POST", headers: headers(extra) });
const errorsOf = (response: { body: unknown }) => (response.body as { errors: string[] }).errors;

beforeEach(() => vi.stubEnv("AP_AGENT_FRONTEND_UPLOAD_MODE", "s3_direct"));
afterEach(() => vi.unstubAllEnvs());

describe("create upload intent", () => {
  it("forwards only the four declared fields with server-built identity and returns the presigned POST", async () => {
    const { deps, calls } = harness(() => intentResponse());
    const response = await handleCreateUploadIntent(createRequest(BODY, { "x-tenant-id": "forged", "x-ap-agent-clerk-authorization": "Bearer forged" }), deps);

    expect(response.status).toBe(201);
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe("https://api.example.on.aws/api/v1/operations/upload-intents");
    const sent = new Headers(calls[0]!.init.headers);
    expect(sent.get("authorization")).toBe("Bearer clerk-session"); // (moved to the dedicated header by upstreamFetch)
    expect(sent.get("x-tenant-id")).toBeNull();
    expect(sent.get("x-ap-agent-clerk-authorization")).toBeNull();
    expect(JSON.parse(calls[0]!.init.body as string)).toEqual(BODY);
  });

  it.each([
    ["not json", "{nope", 400, "MALFORMED_JSON"],
    ["an array", "[]", 400, "MALFORMED_JSON"],
    ["a tenant field", { ...BODY, tenant_id: "x" }, 422, "FIELD_NOT_ALLOWED"],
    ["a role field", { ...BODY, role: "TENANT_ADMIN" }, 422, "FIELD_NOT_ALLOWED"],
    ["a payment field", { ...BODY, bank_account: "x" }, 422, "PAYMENT_FIELD_PROHIBITED"],
    ["a path in the name", { ...BODY, filename: "x.txt" }, 422, "UNSUPPORTED_FILE_TYPE"],
    ["an odd media type", { ...BODY, media_type: "text/html" }, 415, "MEDIA_TYPE_MISMATCH"],
    ["an empty file", { ...BODY, byte_size: 0 }, 422, "EMPTY_FILE"],
    ["a fractional size", { ...BODY, byte_size: 1.5 }, 422, "EMPTY_FILE"],
    ["a too-large file", { ...BODY, byte_size: 10 * 1024 * 1024 + 1 }, 413, "FILE_TOO_LARGE"],
    ["a bad digest", { ...BODY, sha256: "XYZ" }, 422, "SHA256_INVALID"],
  ])("refuses %s before calling the API", async (_label, body, status, code) => {
    const { deps, calls } = harness(() => intentResponse());
    const response = await handleCreateUploadIntent(createRequest(body as never), deps);
    expect(response.status).toBe(status);
    expect(errorsOf(response)).toEqual([code]);
    expect(calls).toHaveLength(0);
  });

  it("accepts the largest supported file", async () => {
    const { deps } = harness(() => intentResponse());
    expect((await handleCreateUploadIntent(createRequest({ ...BODY, byte_size: 10 * 1024 * 1024 }), deps)).status).toBe(201);
  });

  it("rejects cross-site, tokenless and non-JSON requests", async () => {
    const { deps, calls } = harness(() => intentResponse());
    expect(errorsOf(await handleCreateUploadIntent(createRequest(BODY, { origin: "https://evil.example" }), deps))).toEqual(["CSRF_ORIGIN_REJECTED"]);
    expect(errorsOf(await handleCreateUploadIntent(createRequest(BODY, { "x-csrf-token": "bad" }), deps))).toEqual(["CSRF_TOKEN_INVALID"]);
    const form = new Request("https://app.example.on.aws/x", { method: "POST", headers: headers({ "content-type": "text/plain" }), body: "x" });
    expect(errorsOf(await handleCreateUploadIntent(form, deps))).toEqual(["UNSUPPORTED_MEDIA_TYPE"]);
    expect(calls).toHaveLength(0);
  });

  it("is unavailable unless direct upload is configured, and while operations are disabled", async () => {
    vi.stubEnv("AP_AGENT_FRONTEND_UPLOAD_MODE", "");
    const { deps, calls } = harness(() => intentResponse());
    expect(errorsOf(await handleCreateUploadIntent(createRequest(), deps))).toEqual(["DIRECT_UPLOAD_UNAVAILABLE"]);
    vi.stubEnv("AP_AGENT_FRONTEND_UPLOAD_MODE", "s3_direct");
    const off = harness(() => intentResponse(), { loadMode: () => ({ mode: "disabled", valid: true }) });
    expect(errorsOf(await handleCreateUploadIntent(createRequest(), off.deps))).toEqual(["OPERATIONS_DISABLED"]);
    expect(calls.length + off.calls.length).toBe(0);
  });

  it("does not call the API without a signed-in organization member", async () => {
    const { deps, calls } = harness(() => intentResponse(), { authHeaders: async () => ({ ok: false, kind: "SIGN_IN_REQUIRED" }) });
    const response = await handleCreateUploadIntent(createRequest(), deps);
    expect(response.status).toBe(401);
    expect(calls).toHaveLength(0);
  });

  it.each([
    ["an http URL", { upload_url: "http://bucket.s3.amazonaws.com/" }],
    ["a non-Amazon host", { upload_url: "https://evil.example.com/" }],
    ["credentials in the URL", { upload_url: "https://u:p@bucket.s3.amazonaws.com/" }],
    ["non-string fields", { upload_fields: { key: 1 } }],
    ["a bad intent id", { intent_id: "nope" }],
  ])("never hands the browser a presigned POST with %s", async (_label, over) => {
    const { deps } = harness(() => intentResponse(over));
    const response = await handleCreateUploadIntent(createRequest(), deps);
    expect(response.status).toBe(502);
    expect(JSON.stringify(response.body)).not.toContain("evil.example");
  });

  it("passes the API's stable error codes through and redacts everything else", async () => {
    const { deps } = harness(() => json({ request_id: "x", errors: ["ACTION_NOT_PERMITTED", "secret detail with spaces"], generated_at: "x" }, 403));
    const response = await handleCreateUploadIntent(createRequest(), deps);
    expect(response.status).toBe(403);
    expect(errorsOf(response)).toEqual(["ACTION_NOT_PERMITTED"]);
  });
});

describe("finalize upload intent", () => {
  it("forwards a bodiless POST for the exact intent with server-built identity", async () => {
    const { deps, calls } = harness(() => accepted());
    const response = await handleFinalizeUploadIntent(finalizeRequest(), INTENT, deps);

    expect(response.status).toBe(202);
    expect(calls[0]!.url).toBe(`https://api.example.on.aws/api/v1/operations/upload-intents/${INTENT}/finalize`);
    expect(calls[0]!.init.method).toBe("POST");
    expect(new Headers(calls[0]!.init.headers).get("authorization")).toBe("Bearer clerk-session");
  });

  it("returns 200 for an idempotent replay and passes expiry/not-found through", async () => {
    expect((await handleFinalizeUploadIntent(finalizeRequest(), INTENT, harness(() => accepted(200)).deps)).status).toBe(200);
    const gone = harness(() => json({ request_id: "x", errors: ["UPLOAD_INTENT_EXPIRED"], generated_at: "x" }, 410));
    const expired = await handleFinalizeUploadIntent(finalizeRequest(), INTENT, gone.deps);
    expect(expired.status).toBe(410);
    expect(errorsOf(expired)).toEqual(["UPLOAD_INTENT_EXPIRED"]);
  });

  it("refuses a malformed id, a cross-site call and a missing token without calling the API", async () => {
    const { deps, calls } = harness(() => accepted());
    expect((await handleFinalizeUploadIntent(finalizeRequest(), "../../etc", deps)).status).toBe(404);
    expect(errorsOf(await handleFinalizeUploadIntent(finalizeRequest({ origin: "https://evil.example" }), INTENT, deps))).toEqual(["CSRF_ORIGIN_REJECTED"]);
    expect(errorsOf(await handleFinalizeUploadIntent(finalizeRequest({ "x-csrf-token": "" }), INTENT, deps))).toEqual(["CSRF_TOKEN_INVALID"]);
    expect(calls).toHaveLength(0);
  });

  it("rejects an API answer that names no job", async () => {
    const { deps } = harness(() => json({ request_id: "x", status: "ACCEPTED", errors: [], generated_at: "x", data: {} }, 202));
    expect((await handleFinalizeUploadIntent(finalizeRequest(), INTENT, deps)).status).toBe(502);
  });
});

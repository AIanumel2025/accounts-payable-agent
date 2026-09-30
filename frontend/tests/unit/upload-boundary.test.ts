// @vitest-environment node
import { describe, expect, it } from "vitest";
import { MAX_FILE_BYTES, MAX_UPLOAD_REQUEST_BYTES, handleUploadRequest, type UploadDeps } from "@/lib/server/upload-boundary";
import type { ServerEnvConfig } from "@/lib/config/server-env";

const TENANT = "abcdef01-2345-4678-89ab-cdef01234567";
const OP_ID = "5b6f3c1e-8f0a-4c2d-9a11-0c4f0f7a2b11";
const JOB_ID = "77777777-7777-4777-8777-777777777777";
const ENV: ServerEnvConfig = {
  apiBaseUrl: "http://backend.internal:8000", authMode: "development_headers",
  devTenantId: TENANT, devActorId: "operator-x", devActorRole: "AP_OPERATOR",
};

type Call = { url: string; init: RequestInit };

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const accepted = () => json({ request_id: "x", status: "ACCEPTED", data: { idempotent_replay: false, job: { job_id: JOB_ID, status: "QUEUED" } }, errors: [], generated_at: "x" }, 202);

function harness(overrides: Partial<UploadDeps> = {}, upstream: (call: Call) => Response = () => accepted(), backendMode = "ENABLED") {
  const calls: Call[] = [];
  const deps: UploadDeps = {
    loadMode: () => ({ mode: "enabled", valid: true }),
    loadEnv: () => ENV,
    fetchImpl: (async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init: init ?? {} });
      if (url.endsWith("/health")) return json({ request_id: "x", status: "SUCCEEDED", data: { operations_mode: backendMode }, errors: [], generated_at: "x" });
      return upstream({ url, init: init ?? {} });
    }) as typeof fetch,
    now: () => new Date("2026-03-01T12:00:00.000Z"),
    verifyCsrf: (token) => token === "good-token",
    timeoutMs: 500,
    newRequestId: () => "88888888-8888-4888-8888-888888888888",
    ...overrides,
  };
  return { deps, calls };
}

function upload(fields: Record<string, string | File> = {}, headers: Record<string, string> = {}) {
  const form = new FormData();
  form.set("operation_id", OP_ID);
  form.set("file", new File([new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 1, 2, 3])], "invoice.pdf", { type: "application/pdf" }));
  for (const [key, value] of Object.entries(fields)) form.set(key, value);
  return new Request("http://127.0.0.1:3000/api/v1/operations/submissions", {
    method: "POST",
    headers: { origin: "http://127.0.0.1:3000", host: "127.0.0.1:3000", "sec-fetch-site": "same-origin", "x-csrf-token": "good-token", ...headers },
    body: form,
  });
}

const submissionCalls = (calls: Call[]) => calls.filter((call) => call.url.includes("/operations/submissions"));
const errorsOf = (response: { body: unknown }) => (response.body as { errors: string[] }).errors;

describe("upload boundary: gating", () => {
  it("does nothing while operations are disabled or misconfigured", async () => {
    for (const mode of [{ mode: "disabled" as const, valid: true }, { mode: "disabled" as const, valid: false }]) {
      const { deps, calls } = harness({ loadMode: () => mode });
      const response = await handleUploadRequest(upload(), deps);
      expect(response.status).toBe(403);
      expect(errorsOf(response)).toEqual([mode.valid ? "OPERATIONS_DISABLED" : "OPERATIONS_MODE_MISCONFIGURED"]);
      expect(calls).toHaveLength(0);
    }
  });

  it("rejects a cross-origin request, a missing origin and a bad CSRF token before reading the body", async () => {
    for (const headers of [{ origin: "http://evil.example" } as Record<string, string>, { "sec-fetch-site": "cross-site" } as Record<string, string>]) {
      const { deps, calls } = harness();
      expect((await handleUploadRequest(upload({}, headers), deps)).status).toBe(403);
      expect(calls).toHaveLength(0);
    }
    const { deps, calls } = harness();
    const response = await handleUploadRequest(upload({}, { "x-csrf-token": "forged" }), deps);
    expect(errorsOf(response)).toEqual(["CSRF_TOKEN_INVALID"]);
    expect(calls).toHaveLength(0);
  });

  it("accepts only multipart/form-data", async () => {
    const { deps, calls } = harness();
    const request = new Request("http://127.0.0.1:3000/api/v1/operations/submissions", {
      method: "POST",
      headers: { "content-type": "application/json", origin: "http://127.0.0.1:3000", host: "127.0.0.1:3000", "x-csrf-token": "good-token" },
      body: "{}",
    });
    const response = await handleUploadRequest(request, deps);
    expect(response.status).toBe(415);
    expect(calls).toHaveLength(0);
  });

  it("refuses a declared-oversize body up front and a streamed-oversize body without forwarding", async () => {
    const { deps, calls } = harness();
    const declared = upload({}, { "content-length": String(MAX_UPLOAD_REQUEST_BYTES + 1) });
    expect((await handleUploadRequest(declared, deps)).status).toBe(413);

    const big = new File([new Uint8Array(MAX_UPLOAD_REQUEST_BYTES + 10)], "big.pdf", { type: "application/pdf" });
    const response = await handleUploadRequest(upload({ file: big }), deps);
    expect(response.status).toBe(413);
    expect(errorsOf(response)).toEqual(["REQUEST_TOO_LARGE"]);
    expect(calls).toHaveLength(0);
  });
});

describe("upload boundary: fields and files", () => {
  it.each(["tenant_id", "actor_id", "role", "x-tenant-id", "note"])("refuses the unexpected field %s", async (field) => {
    const { deps, calls } = harness();
    const response = await handleUploadRequest(upload({ [field]: "x" }), deps);
    expect(response.status).toBe(422);
    expect(errorsOf(response)).toEqual(["FIELD_NOT_ALLOWED"]);
    expect(calls).toHaveLength(0);
  });

  it.each(["payment_amount", "bank_account", "erp_post", "execute_payment", "IBAN"])("refuses the payment-shaped field %s", async (field) => {
    const { deps, calls } = harness();
    const response = await handleUploadRequest(upload({ [field]: "x" }), deps);
    expect(errorsOf(response)).toEqual(["PAYMENT_FIELD_PROHIBITED"]);
    expect(calls).toHaveLength(0);
  });

  it("requires a UUID operation id and exactly one real file", async () => {
    const { deps } = harness();
    expect(errorsOf(await handleUploadRequest(upload({ operation_id: "not-a-uuid" }), deps))).toEqual(["OPERATION_ID_INVALID"]);
    expect(errorsOf(await handleUploadRequest(upload({ file: "just text" }), deps))).toEqual(["FILE_REQUIRED"]);
  });

  it("fast-fails an empty file, an oversized file and an unsupported extension", async () => {
    const { deps, calls } = harness();
    const empty = new File([], "a.pdf", { type: "application/pdf" });
    expect(errorsOf(await handleUploadRequest(upload({ file: empty }), deps))).toEqual(["EMPTY_FILE"]);
    const text = new File([new Uint8Array([1, 2])], "notes.txt", { type: "text/plain" });
    expect(errorsOf(await handleUploadRequest(upload({ file: text }), deps))).toEqual(["UNSUPPORTED_FILE_TYPE"]);
    expect(MAX_FILE_BYTES).toBe(10 * 1024 * 1024);
    expect(calls).toHaveLength(0);
  });
});

describe("upload boundary: forwarding", () => {
  it("forwards only the file with server-built identity and an operation-derived idempotency key", async () => {
    const { deps, calls } = harness();
    const response = await handleUploadRequest(upload({}, { "x-tenant-id": "forged", "x-actor-role": "TENANT_ADMIN", authorization: "Bearer x" }), deps);

    expect(response.status).toBe(202);
    const [call] = submissionCalls(calls);
    const headers = call!.init.headers as Record<string, string>;
    expect(headers["X-Tenant-ID"]).toBe(TENANT);
    expect(headers["X-Actor-Role"]).toBe("AP_OPERATOR");
    expect(headers["Idempotency-Key"]).toBe(`ap-ui-up-${OP_ID}`);
    expect(Object.keys(headers)).not.toContain("authorization");
    const body = call!.init.body as FormData;
    expect([...body.keys()]).toEqual(["file"]);

    // Regression (found by the real-stack run): the forwarded part is the file's own bytes and type,
    // not the surrounding multipart body.
    const forwarded = body.get("file") as File;
    expect(forwarded.name).toBe("invoice.pdf");
    expect(forwarded.type).toBe("application/pdf");
    expect([...new Uint8Array(await forwarded.arrayBuffer())]).toEqual([0x25, 0x50, 0x44, 0x46, 0x2d, 1, 2, 3]);
  });

  it("fails closed on an operations-mode mismatch and never forwards", async () => {
    for (const backend of ["DISABLED", "SOMETHING_ELSE"]) {
      const { deps, calls } = harness({}, () => accepted(), backend);
      const response = await handleUploadRequest(upload(), deps);
      expect(response.status).toBe(409);
      expect(errorsOf(response)).toEqual(["OPERATIONS_MODE_MISMATCH"]);
      expect(submissionCalls(calls)).toHaveLength(0);
    }
  });

  it("reduces upstream failures to whitelisted codes and never echoes raw text", async () => {
    const { deps } = harness({}, () =>
      json({ request_id: "9b6f3c1e-8f0a-4c2d-9a11-0c4f0f7a2b11", errors: ["FILE_SIGNATURE_MISMATCH", "postgresql://u:p@h/db", "/srv/artifacts/secret path"], generated_at: "x" }, 415),
    );
    const response = await handleUploadRequest(upload(), deps);
    expect(response.status).toBe(415);
    expect(errorsOf(response)).toEqual(["FILE_SIGNATURE_MISMATCH"]);
    expect(JSON.stringify(response.body)).not.toMatch(/postgresql|srv\/artifacts|backend\.internal/);
  });

  it("maps an unreachable backend, a timeout and a malformed response to controlled codes", async () => {
    const down = harness({ fetchImpl: (async () => { throw new TypeError("connect ECONNREFUSED 10.0.0.5:8000"); }) as typeof fetch });
    const unavailable = await handleUploadRequest(upload(), down.deps);
    expect(unavailable.status).toBe(503);
    expect(JSON.stringify(unavailable.body)).not.toContain("10.0.0.5");

    const malformed = harness({}, () => new Response("<html>oops</html>", { status: 202 }));
    expect((await handleUploadRequest(upload(), malformed.deps)).status).toBe(502);
  });

  it("passes an idempotent replay (200) through", async () => {
    const { deps } = harness({}, () => json({ request_id: "x", status: "IDEMPOTENT", data: { idempotent_replay: true, job: { job_id: JOB_ID, status: "QUEUED" } }, errors: [], generated_at: "x" }, 200));
    expect((await handleUploadRequest(upload(), deps)).status).toBe(200);
  });
});

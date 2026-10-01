// @vitest-environment node
import { describe, expect, it } from "vitest";
import type { BackendAuthResult } from "@/lib/auth/backend-auth";
import { handleCommandRequest, type BoundaryDeps } from "@/lib/server/command-boundary";
import { handleUploadRequest, type UploadDeps } from "@/lib/server/upload-boundary";
import type { ServerEnvConfig } from "@/lib/config/server-env";
import { CASE_ID, COMMAND_ID, executedEnvelope } from "../support/command-fixtures";

const ENV: ServerEnvConfig = { apiBaseUrl: "http://private-api.internal:8000", authMode: "clerk_jwt" };
const OP_ID = "5b6f3c1e-8f0a-4c2d-9a11-0c4f0f7a2b11";
const JOB_ID = "77777777-7777-4777-8777-777777777777";

type Call = { url: string; init: RequestInit };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function sameOrigin(extra: Record<string, string> = {}) {
  return { origin: "http://app.example.test", host: "app.example.test", "sec-fetch-site": "same-origin", "x-csrf-token": "good-token", ...extra };
}

function uploadHarness(auth: BackendAuthResult) {
  const calls: Call[] = [];
  const deps: UploadDeps = {
    loadMode: () => ({ mode: "enabled", valid: true }),
    loadEnv: () => ENV,
    fetchImpl: (async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init: init ?? {} });
      if (url.endsWith("/health")) return json({ request_id: "x", status: "SUCCEEDED", data: { operations_mode: "ENABLED" }, errors: [], generated_at: "x" });
      return json({ request_id: "x", status: "ACCEPTED", data: { idempotent_replay: false, job: { job_id: JOB_ID, status: "QUEUED" } }, errors: [], generated_at: "x" }, 202);
    }) as typeof fetch,
    now: () => new Date("2026-03-01T12:00:00.000Z"),
    verifyCsrf: (token) => token === "good-token",
    authHeaders: async () => auth,
    timeoutMs: 500,
    newRequestId: () => "88888888-8888-4888-8888-888888888888",
  };
  return { deps, calls };
}

function uploadRequest(extraHeaders: Record<string, string> = {}) {
  const form = new FormData();
  form.set("operation_id", OP_ID);
  form.set("file", new File([new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 1, 2, 3])], "invoice.pdf", { type: "application/pdf" }));
  return new Request("http://app.example.test/api/v1/operations/submissions", { method: "POST", headers: sameOrigin(extraHeaders), body: form });
}

describe("hosted upload boundary", () => {
  it("forwards only the server-obtained bearer token", async () => {
    const { deps, calls } = uploadHarness({ ok: true, headers: { Authorization: "Bearer server.session.token" } });
    const response = await handleUploadRequest(uploadRequest(), deps);
    expect(response.status).toBe(202);

    const upstream = calls.find((call) => call.url.endsWith("/api/v1/operations/submissions"))!;
    const headers = upstream.init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer server.session.token");
    expect(Object.keys(headers).some((name) => /^x-(tenant|actor|authenticated)/i.test(name))).toBe(false);
  });

  it("ignores identity headers and tokens supplied by the browser", async () => {
    const { deps, calls } = uploadHarness({ ok: true, headers: { Authorization: "Bearer server.session.token" } });
    await handleUploadRequest(
      uploadRequest({ authorization: "Bearer attacker.token", "x-tenant-id": "11111111-1111-1111-1111-111111111111", "x-actor-role": "TENANT_ADMIN", cookie: "__session=abc" }),
      deps,
    );
    const upstream = calls.find((call) => call.url.endsWith("/api/v1/operations/submissions"))!;
    const serialized = JSON.stringify(upstream.init.headers);
    expect(serialized).not.toContain("attacker");
    expect(serialized).not.toMatch(/tenant|actor|cookie|__session/i);
  });

  it.each([
    [{ ok: false, kind: "SIGN_IN_REQUIRED" } as const, 401, "AUTHENTICATION_REQUIRED"],
    [{ ok: false, kind: "ORGANIZATION_REQUIRED" } as const, 403, "ORGANIZATION_REQUIRED"],
    [{ ok: false, kind: "AUTH_UNAVAILABLE" } as const, 503, "AUTHENTICATION_UNAVAILABLE"],
  ])("never forwards an upload without a verified identity (%j)", async (auth, status, code) => {
    const { deps, calls } = uploadHarness(auth);
    const response = await handleUploadRequest(uploadRequest(), deps);
    expect(response.status).toBe(status);
    expect((response.body as { errors: string[] }).errors).toEqual([code]);
    expect(calls.filter((call) => call.url.includes("/submissions"))).toHaveLength(0);
  });

  it("passes backend authentication codes through for the account-state messages", async () => {
    const { deps } = uploadHarness({ ok: true, headers: { Authorization: "Bearer t" } });
    deps.fetchImpl = (async (input: string | URL | Request) =>
      String(input).endsWith("/health")
        ? json({ request_id: "x", status: "SUCCEEDED", data: { operations_mode: "ENABLED" }, errors: [], generated_at: "x" })
        : json({ request_id: "9a3b1c1e-8f0a-4c2d-9a11-0c4f0f7a2b11", errors: ["IDENTITY_MEMBERSHIP_INACTIVE"], generated_at: "x" }, 403)) as typeof fetch;
    const response = await handleUploadRequest(uploadRequest(), deps);
    expect(response.status).toBe(403);
    expect((response.body as { errors: string[] }).errors).toEqual(["IDENTITY_MEMBERSHIP_INACTIVE"]);
  });
});

describe("hosted command boundary", () => {
  function commandHarness(auth: BackendAuthResult) {
    const calls: Call[] = [];
    let tick = 0;
    const deps: BoundaryDeps = {
      loadMode: () => ({ mode: "commit", valid: true }),
      loadEnv: () => ENV,
      fetchImpl: (async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, init: init ?? {} });
        if (url.endsWith("/health")) return json({ request_id: "x", status: "SUCCEEDED", data: { command_mode: "COMMIT" }, errors: [], generated_at: "x" });
        return json(executedEnvelope());
      }) as typeof fetch,
      now: () => new Date(new Date("2026-03-01T12:00:00.000Z").getTime() + tick++),
      verifyCsrf: (token) => token === "good-token",
      authHeaders: async () => auth,
      timeoutMs: 500,
      newRequestId: () => "77777777-7777-4777-8777-777777777777",
    };
    return { deps, calls };
  }

  const body = () =>
    JSON.stringify({
      command_id: COMMAND_ID, idempotency_key: "ap-ui-" + COMMAND_ID, action: "CLAIM", disposition: null,
      observed_review_revision: 1, observed_workflow_revision: 1, reason_codes: [], notes: null, corrections: [],
    });
  const request = (headers: Record<string, string> = {}) =>
    new Request(`http://app.example.test/api/v1/review-cases/${CASE_ID}/commands`, {
      method: "POST", headers: { "content-type": "application/json", ...sameOrigin(), ...headers }, body: body(),
    });

  it("forwards the bearer token and no prototype identity", async () => {
    const { deps, calls } = commandHarness({ ok: true, headers: { Authorization: "Bearer server.session.token" } });
    const response = await handleCommandRequest(request({ authorization: "Bearer attacker", "x-actor-role": "TENANT_ADMIN" }), CASE_ID, deps);
    expect(response.status).toBe(200);
    const upstream = calls.find((call) => call.url.includes("/commands"))!;
    const headers = upstream.init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer server.session.token");
    expect(JSON.stringify(headers)).not.toMatch(/attacker|x-actor|x-tenant/i);
  });

  it("does not send a command without a verified identity", async () => {
    const { deps, calls } = commandHarness({ ok: false, kind: "SIGN_IN_REQUIRED" });
    const response = await handleCommandRequest(request(), CASE_ID, deps);
    expect(response.status).toBe(401);
    expect(calls.filter((call) => call.url.includes("/commands"))).toHaveLength(0);
  });
});

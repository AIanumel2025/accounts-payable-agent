// @vitest-environment node
import { describe, expect, it } from "vitest";
import { authFailureCode, getBackendAuthHeaders } from "@/lib/auth/backend-auth";
import type { ServerEnvConfig } from "@/lib/config/server-env";

const CLERK: ServerEnvConfig = { apiBaseUrl: "http://private-api:8000", authMode: "clerk_jwt" };
const DEV: ServerEnvConfig = {
  apiBaseUrl: "http://private-api:8000", authMode: "development_headers",
  devTenantId: "00000000-0000-0000-0000-000000000000", devActorId: "dev", devActorRole: "AP_OPERATOR",
};
const NOW = new Date("2026-03-01T12:00:00.000Z");

describe("getBackendAuthHeaders", () => {
  it("forwards the session token as a bearer token and nothing else in clerk mode", async () => {
    const result = await getBackendAuthHeaders(CLERK, NOW, async () => ({ userId: "user_1", orgId: "org_1", token: "session.jwt.value" }));
    expect(result).toEqual({ ok: true, headers: { Authorization: "Bearer session.jwt.value" } });
  });

  it("never sends prototype headers in clerk mode", async () => {
    const result = await getBackendAuthHeaders(CLERK, NOW, async () => ({ userId: "u", orgId: "o", token: "t" }));
    if (!result.ok) throw new Error("expected success");
    expect(Object.keys(result.headers)).toEqual(["Authorization"]);
  });

  it("requires a signed-in user with a token", async () => {
    expect(await getBackendAuthHeaders(CLERK, NOW, async () => ({ userId: null, orgId: null, token: null }))).toEqual({ ok: false, kind: "SIGN_IN_REQUIRED" });
    expect(await getBackendAuthHeaders(CLERK, NOW, async () => ({ userId: "u", orgId: "o", token: null }))).toEqual({ ok: false, kind: "SIGN_IN_REQUIRED" });
  });

  it("requires an active organization", async () => {
    expect(await getBackendAuthHeaders(CLERK, NOW, async () => ({ userId: "u", orgId: null, token: "t" }))).toEqual({ ok: false, kind: "ORGANIZATION_REQUIRED" });
    expect(await getBackendAuthHeaders(CLERK, NOW, async () => ({ userId: "u", orgId: undefined, token: "t" }))).toEqual({ ok: false, kind: "ORGANIZATION_REQUIRED" });
  });

  it("fails closed, without leaking the cause, when the session cannot be read", async () => {
    const result = await getBackendAuthHeaders(CLERK, NOW, async () => {
      throw new Error("secret-detail sk_test_abcdefgh");
    });
    expect(result).toEqual({ ok: false, kind: "AUTH_UNAVAILABLE" });
    expect(JSON.stringify(result)).not.toContain("secret-detail");
  });

  it("builds the four development headers only in development mode, without touching the session", async () => {
    const result = await getBackendAuthHeaders(DEV, NOW, async () => {
      throw new Error("must not be called");
    });
    if (!result.ok) throw new Error("expected success");
    expect(result.headers["X-Tenant-ID"]).toBe(DEV.devTenantId);
    expect(result.headers["X-Authenticated-At"]).toBe(NOW.toISOString());
    expect(result.headers.Authorization).toBeUndefined();
  });

  it("maps failures to fixed codes and statuses", () => {
    expect(authFailureCode("SIGN_IN_REQUIRED")).toEqual({ status: 401, code: "AUTHENTICATION_REQUIRED" });
    expect(authFailureCode("ORGANIZATION_REQUIRED")).toEqual({ status: 403, code: "ORGANIZATION_REQUIRED" });
    expect(authFailureCode("AUTH_UNAVAILABLE")).toEqual({ status: 503, code: "AUTHENTICATION_UNAVAILABLE" });
  });
});

describe("callBackend error codes", () => {
  it("reads authentication codes from FastAPI's status-less error envelope", async () => {
    const { callBackend } = await import("@/lib/server/backend-request");
    const original = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ request_id: "r", errors: ["IDENTITY_NOT_MAPPED", "not a code"], generated_at: "x" }), {
        status: 403,
        headers: { "content-type": "application/json" },
      })) as typeof fetch;
    try {
      const result = await callBackend("/api/v1/session", DEV);
      expect(result).toMatchObject({ ok: false, kind: "FORBIDDEN", status: 403, errorCodes: ["IDENTITY_NOT_MAPPED"] });
    } finally {
      globalThis.fetch = original;
    }
  });
});

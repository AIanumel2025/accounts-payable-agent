import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { callBackend } from "@/lib/server/backend-request";
import type { ServerEnvConfig } from "@/lib/config/server-env";

const CONFIG: ServerEnvConfig = {
  apiBaseUrl: "http://127.0.0.1:8000",
  authMode: "development_headers",
  devTenantId: "00000000-0000-0000-0000-000000000000",
  devActorId: "local-reviewer",
  devActorRole: "READ_ONLY_AUDITOR",
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

describe("callBackend", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the envelope's data on success", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(200, { request_id: "r1", status: "SUCCEEDED", data: { total_invoices: 4 }, errors: [], generated_at: "2026-01-01T00:00:00Z" }),
    );

    const result = await callBackend<{ total_invoices: number }>("/api/v1/dashboard", CONFIG);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.data.total_invoices).toBe(4);
  });

  it("maps HTTP 401 to UNAUTHORIZED", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(401, { errors: ["TENANT_ID_MISSING"], generated_at: "2026-01-01T00:00:00Z" }));
    const result = await callBackend("/api/v1/dashboard", CONFIG);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.kind).toBe("UNAUTHORIZED");
  });

  it("maps HTTP 403 to FORBIDDEN", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(403, { errors: ["TENANT_ACCESS_DENIED"], generated_at: "2026-01-01T00:00:00Z" }));
    const result = await callBackend("/api/v1/dashboard", CONFIG);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.kind).toBe("FORBIDDEN");
  });

  it("maps HTTP 503 to UNAVAILABLE", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(503, { errors: ["DATABASE_UNAVAILABLE"], generated_at: "2026-01-01T00:00:00Z" }));
    const result = await callBackend("/api/v1/dashboard", CONFIG);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.kind).toBe("UNAVAILABLE");
  });

  it("treats a network failure as UNAVAILABLE, not a thrown exception", async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError("fetch failed"));
    const result = await callBackend("/api/v1/dashboard", CONFIG);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.kind).toBe("UNAVAILABLE");
  });

  it("treats an abort (timeout) distinctly from a generic network failure", async () => {
    vi.mocked(fetch).mockRejectedValue(new DOMException("The operation was aborted.", "AbortError"));
    const result = await callBackend("/api/v1/dashboard", CONFIG);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.kind).toBe("TIMEOUT");
  });

  it("treats invalid JSON as MALFORMED_RESPONSE", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("not json", { status: 200 }));
    const result = await callBackend("/api/v1/dashboard", CONFIG);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.kind).toBe("MALFORMED_RESPONSE");
  });

  it("treats a 200 with a non-envelope shape as MALFORMED_RESPONSE", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(200, { unexpected: true }));
    const result = await callBackend("/api/v1/dashboard", CONFIG);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.kind).toBe("MALFORMED_RESPONSE");
  });

  it("treats a successful envelope with null data as MALFORMED_RESPONSE, never a silent empty object", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(200, { request_id: "r1", status: "SUCCEEDED", data: null, errors: [], generated_at: "2026-01-01T00:00:00Z" }),
    );
    const result = await callBackend("/api/v1/dashboard", CONFIG);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.kind).toBe("MALFORMED_RESPONSE");
  });

  it("attaches the development-header-authentication headers to every request", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValue(
      jsonResponse(200, { request_id: "r1", status: "SUCCEEDED", data: {}, errors: [], generated_at: "2026-01-01T00:00:00Z" }),
    );
    await callBackend("/health", CONFIG);

    const [, init] = fetchMock.mock.calls[0]!;
    const headers = init!.headers as Record<string, string>;
    expect(headers["X-Tenant-ID"]).toBe(CONFIG.devTenantId);
    expect(headers["X-Actor-ID"]).toBe(CONFIG.devActorId);
    expect(headers["X-Actor-Role"]).toBe(CONFIG.devActorRole);
    expect(headers["X-Authenticated-At"]).toBeDefined();
  });
});

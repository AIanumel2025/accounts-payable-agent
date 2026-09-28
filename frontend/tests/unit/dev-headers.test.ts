import { describe, expect, it } from "vitest";
import { buildDevelopmentAuthHeaders } from "@/lib/auth/dev-headers";
import type { ServerEnvConfig } from "@/lib/config/server-env";

const CONFIG: ServerEnvConfig = {
  apiBaseUrl: "http://127.0.0.1:8000",
  authMode: "development_headers",
  devTenantId: "00000000-0000-0000-0000-000000000000",
  devActorId: "local-reviewer",
  devActorRole: "READ_ONLY_AUDITOR",
};

describe("buildDevelopmentAuthHeaders", () => {
  it("builds exactly the four FastAPI development-header-auth fields", () => {
    const headers = buildDevelopmentAuthHeaders(CONFIG);
    expect(Object.keys(headers).sort()).toEqual(["X-Actor-ID", "X-Actor-Role", "X-Authenticated-At", "X-Tenant-ID"]);
  });

  it("takes tenant/actor identity only from server config, never from anything browser-controlled", () => {
    const headers = buildDevelopmentAuthHeaders(CONFIG);
    expect(headers["X-Tenant-ID"]).toBe(CONFIG.devTenantId);
    expect(headers["X-Actor-ID"]).toBe(CONFIG.devActorId);
    expect(headers["X-Actor-Role"]).toBe(CONFIG.devActorRole);
  });

  it("generates a fresh, timezone-aware ISO-8601 X-Authenticated-At close to now", () => {
    const before = Date.now();
    const headers = buildDevelopmentAuthHeaders(CONFIG);
    const after = Date.now();

    const parsed = new Date(headers["X-Authenticated-At"]!);
    expect(headers["X-Authenticated-At"]).toMatch(/Z$/);
    expect(parsed.getTime()).toBeGreaterThanOrEqual(before);
    expect(parsed.getTime()).toBeLessThanOrEqual(after);
  });

  it("generates a new timestamp on every call", async () => {
    const first = buildDevelopmentAuthHeaders(CONFIG)["X-Authenticated-At"];
    await new Promise((resolve) => setTimeout(resolve, 5));
    const second = buildDevelopmentAuthHeaders(CONFIG)["X-Authenticated-At"];
    expect(first).not.toBe(second);
  });
});

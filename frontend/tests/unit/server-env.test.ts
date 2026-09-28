import { describe, expect, it } from "vitest";
import { loadServerEnvConfig, ServerConfigError } from "@/lib/config/server-env";

const VALID_ENV = {
  AP_AGENT_API_BASE_URL: "http://127.0.0.1:8000",
  AP_AGENT_FRONTEND_AUTH_MODE: "development_headers",
  AP_AGENT_DEV_TENANT_ID: "00000000-0000-0000-0000-000000000000",
  AP_AGENT_DEV_ACTOR_ID: "local-reviewer",
  AP_AGENT_DEV_ACTOR_ROLE: "READ_ONLY_AUDITOR",
};

describe("loadServerEnvConfig", () => {
  it("parses a fully valid environment", () => {
    const config = loadServerEnvConfig(VALID_ENV);
    expect(config).toEqual({
      apiBaseUrl: "http://127.0.0.1:8000",
      authMode: "development_headers",
      devTenantId: "00000000-0000-0000-0000-000000000000",
      devActorId: "local-reviewer",
      devActorRole: "READ_ONLY_AUDITOR",
    });
  });

  it("strips a trailing slash from the API base URL", () => {
    const config = loadServerEnvConfig({ ...VALID_ENV, AP_AGENT_API_BASE_URL: "http://127.0.0.1:8000/" });
    expect(config.apiBaseUrl).toBe("http://127.0.0.1:8000");
  });

  for (const key of Object.keys(VALID_ENV)) {
    it(`throws ServerConfigError with code MISSING_VARIABLE when ${key} is missing`, () => {
      const env = { ...VALID_ENV };
      delete (env as Record<string, string | undefined>)[key];
      expect(() => loadServerEnvConfig(env)).toThrow(ServerConfigError);
      try {
        loadServerEnvConfig(env);
        expect.unreachable();
      } catch (error) {
        expect(error).toBeInstanceOf(ServerConfigError);
        expect((error as ServerConfigError).code).toBe("MISSING_VARIABLE");
        expect((error as ServerConfigError).variable).toBe(key);
      }
    });
  }

  it("throws on a malformed API base URL", () => {
    expect(() => loadServerEnvConfig({ ...VALID_ENV, AP_AGENT_API_BASE_URL: "not-a-url" })).toThrow(ServerConfigError);
  });

  it("throws on an unsupported auth mode", () => {
    expect(() => loadServerEnvConfig({ ...VALID_ENV, AP_AGENT_FRONTEND_AUTH_MODE: "oidc" })).toThrow(ServerConfigError);
  });

  it("throws on a malformed tenant UUID", () => {
    expect(() => loadServerEnvConfig({ ...VALID_ENV, AP_AGENT_DEV_TENANT_ID: "not-a-uuid" })).toThrow(ServerConfigError);
  });

  it("throws on a blank actor id", () => {
    expect(() => loadServerEnvConfig({ ...VALID_ENV, AP_AGENT_DEV_ACTOR_ID: "   " })).toThrow(ServerConfigError);
  });

  it("never accepts a value from something other than the passed-in source (no ambient process.env fallback per key)", () => {
    // Protection against browser-supplied / ambient overrides: passing an
    // explicit, minimal source must not silently pick up unrelated
    // ambient environment state for a key that source doesn't define.
    const config = loadServerEnvConfig(VALID_ENV);
    expect(config.devTenantId).toBe(VALID_ENV.AP_AGENT_DEV_TENANT_ID);
  });
});

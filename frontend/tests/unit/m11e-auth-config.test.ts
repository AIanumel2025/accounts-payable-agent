import { describe, expect, it } from "vitest";
import { ServerConfigError, isHostedEnvironment, loadServerEnvConfig } from "@/lib/config/server-env";

const CLERK_ENV = {
  AP_AGENT_API_BASE_URL: "http://private-api:8000",
  AP_AGENT_FRONTEND_AUTH_MODE: "clerk_jwt",
  NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: "pk_test_ZXhhbXBsZS5jbGVyay5hY2NvdW50cy5kZXYk",
  CLERK_SECRET_KEY: "sk_test_exampleexampleexample",
  AP_AGENT_FRONTEND_CSRF_SECRET: "x".repeat(40),
};

describe("clerk_jwt server configuration (M11E)", () => {
  it("needs no development identity and exposes none", () => {
    const config = loadServerEnvConfig(CLERK_ENV);
    expect(config).toEqual({ apiBaseUrl: "http://private-api:8000", authMode: "clerk_jwt" });
    expect(config.devTenantId).toBeUndefined();
    expect(config.devActorRole).toBeUndefined();
  });

  it.each(["NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "CLERK_SECRET_KEY", "AP_AGENT_FRONTEND_CSRF_SECRET"])("fails closed when %s is missing", (name) => {
    const env: Record<string, string | undefined> = { ...CLERK_ENV };
    delete env[name];
    try {
      loadServerEnvConfig(env);
      expect.unreachable();
    } catch (error) {
      expect(error).toBeInstanceOf(ServerConfigError);
      expect((error as ServerConfigError).code).toBe("MISSING_VARIABLE");
      expect((error as ServerConfigError).variable).toBe(name);
    }
  });

  it.each([
    ["NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "not-a-key"],
    ["NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "sk_test_exampleexampleexample"],
    ["CLERK_SECRET_KEY", "pk_test_ZXhhbXBsZS5jbGVyay5hY2NvdW50cy5kZXYk"],
    ["CLERK_SECRET_KEY", "short"],
    ["AP_AGENT_FRONTEND_CSRF_SECRET", "too-short"],
  ])("rejects a malformed %s", (name, value) => {
    expect(() => loadServerEnvConfig({ ...CLERK_ENV, [name]: value })).toThrow(ServerConfigError);
  });

  it("never echoes a secret value in an error message", () => {
    try {
      loadServerEnvConfig({ ...CLERK_ENV, CLERK_SECRET_KEY: "pk_test_leakyleakyleaky" });
      expect.unreachable();
    } catch (error) {
      expect((error as Error).message).not.toContain("leakyleaky");
    }
  });

  it("refuses development headers in a hosted environment", () => {
    const env = {
      AP_AGENT_API_BASE_URL: "http://private-api:8000",
      AP_AGENT_FRONTEND_AUTH_MODE: "development_headers",
      AP_AGENT_DEV_TENANT_ID: "00000000-0000-0000-0000-000000000000",
      AP_AGENT_DEV_ACTOR_ID: "x",
      AP_AGENT_DEV_ACTOR_ROLE: "TENANT_ADMIN",
      AP_AGENT_ENVIRONMENT: "hosted",
    };
    expect(isHostedEnvironment(env)).toBe(true);
    expect(() => loadServerEnvConfig(env)).toThrow(ServerConfigError);
    expect(() => loadServerEnvConfig({ ...env, AP_AGENT_ENVIRONMENT: "development" })).not.toThrow();
  });

  it("still rejects unknown auth modes", () => {
    expect(() => loadServerEnvConfig({ ...CLERK_ENV, AP_AGENT_FRONTEND_AUTH_MODE: "oidc" })).toThrow(ServerConfigError);
  });
});

import "server-only";

/**
 * Server-only environment configuration (M11A task §5).
 *
 * Every value here is read exclusively by server-side code (Server
 * Components, Route Handlers). None of these variable names are prefixed
 * `NEXT_PUBLIC_`, so Next.js never inlines them into a browser bundle.
 * Nothing in this module is exported to client components.
 */

export class ServerConfigError extends Error {
  readonly code: "MISSING_VARIABLE" | "INVALID_VALUE";
  readonly variable: string;

  constructor(code: "MISSING_VARIABLE" | "INVALID_VALUE", variable: string, message: string) {
    super(message);
    this.name = "ServerConfigError";
    this.code = code;
    this.variable = variable;
  }
}

export type FrontendAuthMode = "development_headers";

export interface ServerEnvConfig {
  apiBaseUrl: string;
  authMode: FrontendAuthMode;
  devTenantId: string;
  devActorId: string;
  devActorRole: string;
}

const UUID_PATTERN = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

export type EnvSource = Record<string, string | undefined>;

function requireVariable(source: EnvSource, name: string): string {
  const value = source[name];
  if (value === undefined || value.trim() === "") {
    throw new ServerConfigError("MISSING_VARIABLE", name, `Required server-only environment variable ${name} is not set.`);
  }
  return value;
}

function assertValidBaseUrl(name: string, value: string): string {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new ServerConfigError("INVALID_VALUE", name, `${name} must be an absolute URL.`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new ServerConfigError("INVALID_VALUE", name, `${name} must use http or https.`);
  }
  return value.replace(/\/$/, "");
}

function assertValidAuthMode(name: string, value: string): FrontendAuthMode {
  if (value !== "development_headers") {
    throw new ServerConfigError(
      "INVALID_VALUE",
      name,
      `${name} must be "development_headers" in M11A (no production identity-provider integration exists yet).`,
    );
  }
  return value;
}

function assertValidUuid(name: string, value: string): string {
  if (!UUID_PATTERN.test(value)) {
    throw new ServerConfigError("INVALID_VALUE", name, `${name} must be a valid UUID.`);
  }
  return value;
}

function assertNonEmpty(name: string, value: string): string {
  if (value.trim().length === 0) {
    throw new ServerConfigError("INVALID_VALUE", name, `${name} must not be empty.`);
  }
  return value;
}

/**
 * Reads and validates every server-only environment variable this
 * application needs. Throws `ServerConfigError` (never a bare `Error`) on a
 * missing or malformed variable so callers can render a clear
 * "authentication misconfigured" state (task §10) instead of a stack trace.
 *
 * Accepts an explicit `source` only for unit testing; production code
 * always calls this with no argument, reading `process.env`.
 */
export function loadServerEnvConfig(source: EnvSource = process.env): ServerEnvConfig {
  const apiBaseUrl = assertValidBaseUrl("AP_AGENT_API_BASE_URL", requireVariable(source, "AP_AGENT_API_BASE_URL"));
  const authMode = assertValidAuthMode(
    "AP_AGENT_FRONTEND_AUTH_MODE",
    requireVariable(source, "AP_AGENT_FRONTEND_AUTH_MODE"),
  );
  const devTenantId = assertValidUuid("AP_AGENT_DEV_TENANT_ID", requireVariable(source, "AP_AGENT_DEV_TENANT_ID"));
  const devActorId = assertNonEmpty("AP_AGENT_DEV_ACTOR_ID", requireVariable(source, "AP_AGENT_DEV_ACTOR_ID"));
  const devActorRole = assertNonEmpty(
    "AP_AGENT_DEV_ACTOR_ROLE",
    requireVariable(source, "AP_AGENT_DEV_ACTOR_ROLE"),
  );

  return { apiBaseUrl, authMode, devTenantId, devActorId, devActorRole };
}

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

export type FrontendAuthMode = "development_headers" | "clerk_jwt";

/**
 * `development_headers` (local development and automated tests only): the
 * server builds the four prototype headers from the `AP_AGENT_DEV_*`
 * variables. `clerk_jwt` (hosted): the server forwards the Clerk session
 * token as a bearer token and none of the `AP_AGENT_DEV_*` values exist.
 */
export interface ServerEnvConfig {
  apiBaseUrl: string;
  authMode: FrontendAuthMode;
  /** Present only in `development_headers` mode. */
  devTenantId?: string;
  devActorId?: string;
  devActorRole?: string;
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
  if (value !== "development_headers" && value !== "clerk_jwt") {
    throw new ServerConfigError("INVALID_VALUE", name, `${name} must be "development_headers" or "clerk_jwt".`);
  }
  return value;
}

const CLERK_PUBLISHABLE_KEY_PATTERN = /^pk_(test|live)_[A-Za-z0-9+/=_-]{8,}$/;
const CLERK_SECRET_KEY_PATTERN = /^sk_(test|live)_[A-Za-z0-9_-]{8,}$/;

/** True when `AP_AGENT_ENVIRONMENT` marks this deployment as hosted. */
export function isHostedEnvironment(source: EnvSource = process.env): boolean {
  return (source.AP_AGENT_ENVIRONMENT ?? "").trim().toLowerCase() === "hosted";
}

/**
 * Hosted startup must fail closed: prototype headers are refused, and the
 * Clerk configuration (public key, secret key, request-forgery secret) must be
 * present and well formed. Messages name variables, never values.
 */
function assertClerkConfiguration(source: EnvSource): void {
  const publishable = requireVariable(source, "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY");
  if (!CLERK_PUBLISHABLE_KEY_PATTERN.test(publishable)) {
    throw new ServerConfigError("INVALID_VALUE", "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY is malformed.");
  }
  const secret = requireVariable(source, "CLERK_SECRET_KEY");
  if (!CLERK_SECRET_KEY_PATTERN.test(secret)) {
    throw new ServerConfigError("INVALID_VALUE", "CLERK_SECRET_KEY", "CLERK_SECRET_KEY is malformed.");
  }
  const csrf = requireVariable(source, "AP_AGENT_FRONTEND_CSRF_SECRET");
  if (csrf.length < 32) {
    throw new ServerConfigError("INVALID_VALUE", "AP_AGENT_FRONTEND_CSRF_SECRET", "AP_AGENT_FRONTEND_CSRF_SECRET must be at least 32 characters.");
  }
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

  if (authMode === "clerk_jwt") {
    assertClerkConfiguration(source);
    return { apiBaseUrl, authMode };
  }

  if (isHostedEnvironment(source)) {
    throw new ServerConfigError(
      "INVALID_VALUE",
      "AP_AGENT_FRONTEND_AUTH_MODE",
      'AP_AGENT_FRONTEND_AUTH_MODE must be "clerk_jwt" in a hosted environment.',
    );
  }

  const devTenantId = assertValidUuid("AP_AGENT_DEV_TENANT_ID", requireVariable(source, "AP_AGENT_DEV_TENANT_ID"));
  const devActorId = assertNonEmpty("AP_AGENT_DEV_ACTOR_ID", requireVariable(source, "AP_AGENT_DEV_ACTOR_ID"));
  const devActorRole = assertNonEmpty(
    "AP_AGENT_DEV_ACTOR_ROLE",
    requireVariable(source, "AP_AGENT_DEV_ACTOR_ROLE"),
  );

  return { apiBaseUrl, authMode, devTenantId, devActorId, devActorRole };
}

import "server-only";
import { buildDevelopmentAuthHeaders } from "@/lib/auth/dev-headers";
import type { ServerEnvConfig } from "@/lib/config/server-env";

/**
 * Server-only FastAPI request boundary (M11A task §5/§10).
 *
 * Every outbound call carries only the four development/test
 * authentication headers this application constructs itself -- it never
 * forwards arbitrary browser-supplied headers (task §5: "The Next.js
 * server must not forward arbitrary browser-supplied authentication
 * headers.").
 */

// Overridable only for tests (`AP_AGENT_BACKEND_TIMEOUT_MS`), so the
// Playwright timeout scenario doesn't have to wait out a production-sized
// timeout; unset in every real deployment, where the 8s default applies.
const REQUEST_TIMEOUT_MS = Number(process.env.AP_AGENT_BACKEND_TIMEOUT_MS ?? 8_000);

export type BackendErrorKind =
  | "UNAUTHORIZED"
  | "FORBIDDEN"
  | "NOT_FOUND"
  | "TIMEOUT"
  | "UNAVAILABLE"
  | "MALFORMED_RESPONSE"
  | "UNKNOWN";

export interface BackendSuccess<T> {
  ok: true;
  status: number;
  data: T;
}

export interface BackendFailure {
  ok: false;
  kind: BackendErrorKind;
  status?: number;
  /** A message safe to show a user: never a stack trace, SQL, DSN, hostname or filesystem path (task §10). */
  message: string;
}

export type BackendResult<T> = BackendSuccess<T> | BackendFailure;

interface ApiEnvelopeShape {
  request_id: string;
  status: string;
  data: unknown;
  errors: string[];
  generated_at: string;
}

function isApiEnvelopeShape(value: unknown): value is ApiEnvelopeShape {
  return (
    typeof value === "object" &&
    value !== null &&
    "status" in value &&
    "generated_at" in value &&
    "errors" in value &&
    Array.isArray((value as { errors: unknown }).errors)
  );
}

function statusToErrorKind(status: number): BackendErrorKind {
  if (status === 401) return "UNAUTHORIZED";
  if (status === 403) return "FORBIDDEN";
  if (status === 404) return "NOT_FOUND";
  if (status === 503) return "UNAVAILABLE";
  return "UNKNOWN";
}

/**
 * Calls a single M10 FastAPI endpoint and returns a typed result -- never
 * throws for an ordinary network/HTTP failure, so callers can render the
 * required interface states (task §10) without a try/catch at every call
 * site. `path` must start with `/`, e.g. `/api/v1/dashboard`.
 */
export async function callBackend<T>(
  path: string,
  config: ServerEnvConfig,
  init?: { searchParams?: Record<string, string> },
): Promise<BackendResult<T>> {
  const url = new URL(config.apiBaseUrl + path);
  for (const [key, value] of Object.entries(init?.searchParams ?? {})) {
    url.searchParams.set(key, value);
  }

  const headers = buildDevelopmentAuthHeaders(config);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  let response: Response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers,
      signal: controller.signal,
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      return { ok: false, kind: "TIMEOUT", message: "The backend did not respond in time. Please retry." };
    }
    return { ok: false, kind: "UNAVAILABLE", message: "The backend service is currently unreachable. Please retry." };
  } finally {
    clearTimeout(timeout);
  }

  let parsed: unknown;
  try {
    parsed = await response.json();
  } catch {
    return {
      ok: false,
      kind: "MALFORMED_RESPONSE",
      status: response.status,
      message: "The backend returned a response that could not be understood.",
    };
  }

  if (!response.ok) {
    const errors = isApiEnvelopeShape(parsed) ? parsed.errors : [];
    return {
      ok: false,
      kind: statusToErrorKind(response.status),
      status: response.status,
      message: errors.length > 0 ? errors.join(", ") : `Backend request failed with status ${response.status}.`,
    };
  }

  if (!isApiEnvelopeShape(parsed)) {
    return {
      ok: false,
      kind: "MALFORMED_RESPONSE",
      status: response.status,
      message: "The backend returned an unexpected response shape.",
    };
  }

  if (parsed.data === null || parsed.data === undefined) {
    return {
      ok: false,
      kind: "MALFORMED_RESPONSE",
      status: response.status,
      message: "The backend returned a successful response with no data.",
    };
  }

  return { ok: true, status: response.status, data: parsed.data as T };
}

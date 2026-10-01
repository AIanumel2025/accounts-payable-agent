import "server-only";
import { randomUUID } from "node:crypto";
import { authFailureCode, getBackendAuthHeaders, type BackendAuthResult } from "@/lib/auth/backend-auth";
import { loadCommandMode, modesAgree, type CommandModeConfig } from "@/lib/config/command-mode";
import { loadServerEnvConfig, ServerConfigError, type ServerEnvConfig } from "@/lib/config/server-env";
import {
  MAX_REQUEST_BODY_BYTES,
  UUID_PATTERN,
  isRecord,
  parseBrowserCommandRequest,
  parseCommandFailure,
  parseCommandSuccess,
} from "@/lib/commands/contract";
import { CSRF_HEADER, isSameOriginRequest, verifyCsrfToken } from "@/lib/server/csrf";

/**
 * The single, server-side review-command boundary (M11C task §7).
 *
 * Exactly one browser-reachable write exists:
 * `POST /api/v1/review-cases/{uuid}/commands`. This function is everything
 * that route does; it takes its collaborators as arguments so every branch
 * is unit-testable without Next.js.
 *
 * Nothing identity-shaped is ever taken from the browser: tenant, actor,
 * role, `X-Authenticated-At` and `requested_at` are all constructed here,
 * from server-only configuration and the server clock. Upstream failures are
 * reduced to a whitelist of error *codes*; raw upstream text, SQL, DSNs,
 * hostnames and paths cannot reach the browser through this path.
 */

export interface BoundaryDeps {
  loadMode: () => CommandModeConfig;
  loadEnv: () => ServerEnvConfig;
  fetchImpl: typeof fetch;
  now: () => Date;
  verifyCsrf: (token: string | null, reviewCaseId: string) => boolean;
  /** Identity presented to FastAPI (M11E). Defaults to development headers or the Clerk bearer token, by auth mode. */
  authHeaders?: (config: ServerEnvConfig, now: Date) => Promise<BackendAuthResult>;
  timeoutMs: number;
  newRequestId: () => string;
}

export function defaultBoundaryDeps(): BoundaryDeps {
  return {
    loadMode: () => loadCommandMode(),
    loadEnv: () => loadServerEnvConfig(),
    fetchImpl: (input, init) => fetch(input, init),
    now: () => new Date(),
    verifyCsrf: (token, reviewCaseId) => verifyCsrfToken(token, reviewCaseId),
    authHeaders: (config, now) => getBackendAuthHeaders(config, now),
    timeoutMs: Number(process.env.AP_AGENT_BACKEND_TIMEOUT_MS ?? 8_000),
    newRequestId: () => randomUUID(),
  };
}

export interface BoundaryResponse {
  status: number;
  body: unknown;
}

const PASSTHROUGH_STATUSES = new Set([401, 403, 404, 409, 422, 500, 503]);

const DEFAULT_CODE_BY_STATUS: Record<number, string> = {
  401: "AUTHENTICATION_FAILED",
  403: "ACTION_NOT_PERMITTED",
  404: "REVIEW_CASE_NOT_FOUND",
  409: "STALE_REVIEW_REVISION",
  422: "REQUEST_VALIDATION_FAILED",
  500: "INTERNAL_ERROR",
  503: "DATABASE_UNAVAILABLE",
};

function failure(deps: BoundaryDeps, status: number, errors: string[], requestId?: string | null): BoundaryResponse {
  return {
    status,
    body: { request_id: requestId ?? deps.newRequestId(), errors, generated_at: deps.now().toISOString() },
  };
}

function isJsonContentType(value: string | null): boolean {
  if (value === null) return false;
  const [mediaType] = value.split(";");
  return mediaType?.trim().toLowerCase() === "application/json";
}

/** Reads at most `limit` bytes; resolves `null` as soon as the cap is exceeded (never buffers an oversized body). */
async function readCappedBody(request: Request, limit: number): Promise<string | null> {
  const declared = request.headers.get("content-length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > limit)) return null;
  if (request.body === null) return "";

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    received += value.byteLength;
    if (received > limit) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  return new TextDecoder("utf-8", { fatal: false }).decode(Buffer.concat(chunks));
}

async function callUpstream(
  deps: BoundaryDeps,
  url: string,
  init: RequestInit,
): Promise<{ ok: true; response: Response } | { ok: false; code: "BACKEND_TIMEOUT" | "BACKEND_UNAVAILABLE" }> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), deps.timeoutMs);
  try {
    const response = await deps.fetchImpl(url, { ...init, signal: controller.signal, cache: "no-store" });
    return { ok: true, response };
  } catch (error) {
    const aborted = error instanceof DOMException && error.name === "AbortError";
    return { ok: false, code: aborted ? "BACKEND_TIMEOUT" : "BACKEND_UNAVAILABLE" };
  } finally {
    clearTimeout(timeout);
  }
}

export async function handleCommandRequest(
  request: Request,
  reviewCaseId: string,
  deps: BoundaryDeps,
): Promise<BoundaryResponse> {
  // 0. The path parameter must be a real UUID (rejects traversal / junk before anything else).
  if (!UUID_PATTERN.test(reviewCaseId)) return failure(deps, 404, ["REVIEW_CASE_NOT_FOUND"]);

  // 1. Mode: disabled (or misconfigured) never forwards anything, whatever the backend mode is.
  const modeConfig = deps.loadMode();
  if (!modeConfig.valid) return failure(deps, 403, ["COMMAND_MODE_MISCONFIGURED"]);
  if (modeConfig.mode === "disabled") return failure(deps, 403, ["REVIEW_COMMANDS_DISABLED"]);

  // 2. CSRF layers: same origin, JSON only, per-page token.
  if (!isSameOriginRequest(request.headers)) return failure(deps, 403, ["CSRF_ORIGIN_REJECTED"]);
  if (!isJsonContentType(request.headers.get("content-type"))) return failure(deps, 415, ["UNSUPPORTED_MEDIA_TYPE"]);
  if (!deps.verifyCsrf(request.headers.get(CSRF_HEADER), reviewCaseId)) return failure(deps, 403, ["CSRF_TOKEN_INVALID"]);

  // 3. Bounded body, safe JSON parse, allow-listed fields.
  const text = await readCappedBody(request, MAX_REQUEST_BODY_BYTES);
  if (text === null) return failure(deps, 413, ["REQUEST_TOO_LARGE"]);

  let parsedBody: unknown;
  try {
    parsedBody = JSON.parse(text);
  } catch {
    return failure(deps, 400, ["MALFORMED_JSON"]);
  }

  const parsed = parseBrowserCommandRequest(parsedBody);
  if (!parsed.ok) return failure(deps, 422, ["COMMAND_REQUEST_INVALID", ...parsed.errors]);

  // 4. Server configuration.
  let config: ServerEnvConfig;
  try {
    config = deps.loadEnv();
  } catch (error) {
    if (error instanceof ServerConfigError) return failure(deps, 500, ["FRONTEND_AUTH_MISCONFIGURED"]);
    throw error;
  }

  // 5. Fail closed on any frontend/backend mode disagreement (or an unreadable backend mode).
  const health = await callUpstream(deps, `${config.apiBaseUrl}/health`, { method: "GET" });
  if (!health.ok) return failure(deps, 503, [health.code]);
  let backendMode: string | null = null;
  try {
    const healthBody: unknown = await health.response.json();
    if (isRecord(healthBody) && isRecord(healthBody.data) && typeof healthBody.data.command_mode === "string") {
      backendMode = healthBody.data.command_mode;
    }
  } catch {
    backendMode = null;
  }
  if (!modesAgree(modeConfig.mode, backendMode)) return failure(deps, 409, ["COMMAND_MODE_MISMATCH"]);

  // 6. Server-built identity and timestamps. `requested_at` is taken *after* the
  //    authentication timestamp so `authenticated_at <= requested_at` always holds.
  const authenticatedAt = deps.now();
  const auth = await (deps.authHeaders ?? getBackendAuthHeaders)(config, authenticatedAt);
  if (!auth.ok) {
    const authFailure = authFailureCode(auth.kind);
    return failure(deps, authFailure.status, [authFailure.code]);
  }
  const headers = {
    ...auth.headers,
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  const requestedAt = deps.now();
  const upstreamBody = JSON.stringify({
    ...parsed.value,
    requested_at: (requestedAt < authenticatedAt ? authenticatedAt : requestedAt).toISOString(),
  });

  const upstream = await callUpstream(deps, `${config.apiBaseUrl}/api/v1/review-cases/${reviewCaseId.toLowerCase()}/commands`, {
    method: "POST",
    headers,
    body: upstreamBody,
  });
  if (!upstream.ok) return failure(deps, 503, [upstream.code]);

  let upstreamJson: unknown;
  try {
    upstreamJson = await upstream.response.json();
  } catch {
    return failure(deps, 502, ["MALFORMED_RESPONSE"]);
  }

  if (upstream.response.status === 200) {
    const success = parseCommandSuccess(upstreamJson);
    if (success === null) return failure(deps, 502, ["MALFORMED_RESPONSE"]);
    return { status: 200, body: upstreamJson };
  }

  const problem = parseCommandFailure(upstreamJson);
  const status = PASSTHROUGH_STATUSES.has(upstream.response.status) ? upstream.response.status : 502;
  const errors = problem.errors.length > 0 ? problem.errors : [DEFAULT_CODE_BY_STATUS[status] ?? "INTERNAL_ERROR"];
  return failure(deps, status, errors, problem.requestId);
}

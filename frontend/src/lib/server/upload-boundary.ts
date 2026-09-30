import "server-only";
import { randomUUID } from "node:crypto";
import { buildDevelopmentAuthHeaders } from "@/lib/auth/dev-headers";
import { loadOperationsMode, operationsModesAgree, type OperationsModeConfig } from "@/lib/config/operations-mode";
import { loadServerEnvConfig, ServerConfigError, type ServerEnvConfig } from "@/lib/config/server-env";
import { UUID_PATTERN, isRecord } from "@/lib/commands/contract";
import { CSRF_HEADER, isSameOriginRequest, verifyCsrfToken } from "@/lib/server/csrf";

/**
 * The single, server-side upload boundary (M11D Core): the only browser
 * route that accepts a file. `POST /api/v1/operations/submissions`.
 *
 * Enforced here, in order: operations mode, same-origin, multipart content
 * type, per-page CSRF token, a *streamed* byte cap (an oversized body is
 * never buffered), field allow-listing (`file` and `operation_id` only --
 * tenant/actor/role/payment-shaped fields are refused), a fast-fail file
 * check, and a frontend/backend operations-mode agreement check. Identity is
 * never taken from the browser: tenant, actor and role headers are built
 * here from server-only configuration. FastAPI remains the authority on
 * file validation; this layer only refuses early and redacts errors to a
 * whitelist of stable codes.
 */

export const UPLOAD_CSRF_SCOPE = "operations-upload";
export const MAX_FILE_BYTES = 10 * 1024 * 1024;
export const MAX_UPLOAD_REQUEST_BYTES = MAX_FILE_BYTES + 64 * 1024;
const ALLOWED_EXTENSIONS = [".pdf", ".png", ".jpg", ".jpeg"];
const ALLOWED_FIELDS = new Set(["file", "operation_id"]);
const PAYMENT_FIELD_PATTERN = /(pay|bank|erp|iban|swift|routing|account[_-]?number|transfer|wire|post[_-]?to)/i;
const ERROR_CODE_PATTERN = /^[A-Z][A-Z0-9_:.-]{2,80}$/;
const PASSTHROUGH_STATUSES = new Set([401, 403, 404, 409, 413, 415, 422, 500, 503]);

export interface UploadDeps {
  loadMode: () => OperationsModeConfig;
  loadEnv: () => ServerEnvConfig;
  fetchImpl: typeof fetch;
  now: () => Date;
  verifyCsrf: (token: string | null) => boolean;
  timeoutMs: number;
  newRequestId: () => string;
}

export function defaultUploadDeps(): UploadDeps {
  return {
    loadMode: () => loadOperationsMode(),
    loadEnv: () => loadServerEnvConfig(),
    fetchImpl: (input, init) => fetch(input, init),
    now: () => new Date(),
    verifyCsrf: (token) => verifyCsrfToken(token, UPLOAD_CSRF_SCOPE),
    timeoutMs: Number(process.env.AP_AGENT_BACKEND_TIMEOUT_MS ?? 8_000),
    newRequestId: () => randomUUID(),
  };
}

export interface UploadResponse {
  status: number;
  body: unknown;
}

function failure(deps: UploadDeps, status: number, errors: string[], requestId?: string | null): UploadResponse {
  return { status, body: { request_id: requestId ?? deps.newRequestId(), errors, generated_at: deps.now().toISOString() } };
}

/** Reads at most `limit` bytes; resolves `null` the moment the cap is exceeded. */
async function readCappedBytes(request: Request, limit: number): Promise<Uint8Array<ArrayBuffer> | null> {
  const declared = request.headers.get("content-length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > limit)) return null;
  if (request.body === null) return new Uint8Array(new ArrayBuffer(0));

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
  const out = new Uint8Array(new ArrayBuffer(received));
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return out;
}

async function callUpstream(
  deps: UploadDeps,
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

function safeErrorCodes(body: unknown): { errors: string[]; requestId: string | null } {
  if (!isRecord(body)) return { errors: [], requestId: null };
  const errors = Array.isArray(body.errors)
    ? body.errors.filter((code): code is string => typeof code === "string" && ERROR_CODE_PATTERN.test(code))
    : [];
  const requestId = typeof body.request_id === "string" && UUID_PATTERN.test(body.request_id) ? body.request_id : null;
  return { errors, requestId };
}

export async function handleUploadRequest(request: Request, deps: UploadDeps): Promise<UploadResponse> {
  // 1. Mode: disabled (or misconfigured) never forwards anything.
  const mode = deps.loadMode();
  if (!mode.valid) return failure(deps, 403, ["OPERATIONS_MODE_MISCONFIGURED"]);
  if (mode.mode === "disabled") return failure(deps, 403, ["OPERATIONS_DISABLED"]);

  // 2. CSRF layers: same origin, multipart only, per-page token.
  if (!isSameOriginRequest(request.headers)) return failure(deps, 403, ["CSRF_ORIGIN_REJECTED"]);
  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().startsWith("multipart/form-data")) return failure(deps, 415, ["UNSUPPORTED_MEDIA_TYPE"]);
  if (!deps.verifyCsrf(request.headers.get(CSRF_HEADER))) return failure(deps, 403, ["CSRF_TOKEN_INVALID"]);

  // 3. Bounded body (never buffers an oversized upload), safe multipart parse.
  const bytes = await readCappedBytes(request, MAX_UPLOAD_REQUEST_BYTES);
  if (bytes === null) return failure(deps, 413, ["REQUEST_TOO_LARGE"]);

  let form: FormData;
  try {
    form = await new Response(new Blob([bytes]), { headers: { "content-type": contentType } }).formData();
  } catch {
    return failure(deps, 400, ["MALFORMED_MULTIPART"]);
  }

  // 4. Field allow-list: nothing identity- or payment-shaped can ride along.
  const names = [...form.keys()];
  if (names.some((name) => PAYMENT_FIELD_PATTERN.test(name))) return failure(deps, 422, ["PAYMENT_FIELD_PROHIBITED"]);
  if (names.some((name) => !ALLOWED_FIELDS.has(name))) return failure(deps, 422, ["FIELD_NOT_ALLOWED"]);

  const operationId = form.get("operation_id");
  if (typeof operationId !== "string" || !UUID_PATTERN.test(operationId)) return failure(deps, 422, ["OPERATION_ID_INVALID"]);

  const files = form.getAll("file");
  const file = files[0];
  if (files.length !== 1 || !(file instanceof File)) return failure(deps, 422, ["FILE_REQUIRED"]);

  // 5. Fast-fail file checks (FastAPI re-validates everything authoritatively).
  if (file.size === 0) return failure(deps, 422, ["EMPTY_FILE"]);
  if (file.size > MAX_FILE_BYTES) return failure(deps, 413, ["FILE_TOO_LARGE"]);
  const lowerName = file.name.toLowerCase();
  if (!ALLOWED_EXTENSIONS.some((extension) => lowerName.endsWith(extension))) return failure(deps, 422, ["UNSUPPORTED_FILE_TYPE"]);

  // 6. Server configuration.
  let config: ServerEnvConfig;
  try {
    config = deps.loadEnv();
  } catch (error) {
    if (error instanceof ServerConfigError) return failure(deps, 500, ["FRONTEND_AUTH_MISCONFIGURED"]);
    throw error;
  }

  // 7. Fail closed on any frontend/backend operations-mode disagreement.
  const health = await callUpstream(deps, `${config.apiBaseUrl}/health`, { method: "GET" });
  if (!health.ok) return failure(deps, 503, [health.code]);
  let backendMode: string | null = null;
  try {
    const healthBody: unknown = await health.response.json();
    if (isRecord(healthBody) && isRecord(healthBody.data) && typeof healthBody.data.operations_mode === "string") {
      backendMode = healthBody.data.operations_mode;
    }
  } catch {
    backendMode = null;
  }
  if (!operationsModesAgree(mode.mode, backendMode)) return failure(deps, 409, ["OPERATIONS_MODE_MISMATCH"]);

  // 8. Server-built identity; only the file is forwarded, re-encoded.
  const upstreamForm = new FormData();
  upstreamForm.set("file", file, file.name);

  const authHeaders = buildDevelopmentAuthHeaders(config, deps.now());
  const upstream = await callUpstream(deps, `${config.apiBaseUrl}/api/v1/operations/submissions`, {
    method: "POST",
    headers: { ...authHeaders, Accept: "application/json", "Idempotency-Key": `ap-ui-up-${operationId.toLowerCase()}` },
    body: upstreamForm,
  });
  if (!upstream.ok) return failure(deps, 503, [upstream.code]);

  let upstreamJson: unknown;
  try {
    upstreamJson = await upstream.response.json();
  } catch {
    return failure(deps, 502, ["MALFORMED_RESPONSE"]);
  }

  if (upstream.response.status === 200 || upstream.response.status === 202) {
    const data = isRecord(upstreamJson) && isRecord(upstreamJson.data) ? upstreamJson.data : null;
    const job = data !== null && isRecord(data.job) ? data.job : null;
    if (job === null || typeof job.job_id !== "string" || !UUID_PATTERN.test(job.job_id)) {
      return failure(deps, 502, ["MALFORMED_RESPONSE"]);
    }
    return { status: upstream.response.status, body: upstreamJson };
  }

  const problem = safeErrorCodes(upstreamJson);
  const status = PASSTHROUGH_STATUSES.has(upstream.response.status) ? upstream.response.status : 502;
  return failure(deps, status, problem.errors.length > 0 ? problem.errors : ["REQUEST_NOT_ACCEPTED"], problem.requestId);
}

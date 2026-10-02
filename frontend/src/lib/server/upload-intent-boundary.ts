import "server-only";
import { authFailureCode, getBackendAuthHeaders } from "@/lib/auth/backend-auth";
import { loadUploadMode } from "@/lib/config/operations-mode";
import { UUID_PATTERN, isRecord } from "@/lib/commands/contract";
import { ServerConfigError, type ServerEnvConfig } from "@/lib/config/server-env";
import { CSRF_HEADER, isSameOriginRequest } from "@/lib/server/csrf";
import {
  MAX_FILE_BYTES,
  callUpstream,
  failure,
  readCappedBytes,
  safeErrorCodes,
  type UploadDeps,
  type UploadResponse,
} from "@/lib/server/upload-boundary";

/**
 * The two server-side boundaries of a staged direct-to-S3 upload (M11E.1; AWS mode):
 *
 *   POST /api/v1/operations/upload-intents                   -> FastAPI issues a presigned POST
 *   POST /api/v1/operations/upload-intents/{id}/finalize     -> FastAPI validates the stored object,
 *                                                               creates the job and enqueues it
 *
 * The file itself never passes through this server or through Lambda. Everything here is small JSON.
 * Enforced in order: operations + upload mode, same origin, per-page CSRF token, a tiny capped body,
 * an exact field allow-list (tenant/actor/role/payment-shaped fields are refused), fast-fail file
 * checks, and server-built identity (the browser's own headers are never forwarded; FastAPI verifies
 * the Clerk session token itself and resolves tenant and role from its database). Responses are
 * validated before the presigned POST reaches the browser: it must be an https Amazon S3 URL.
 */

export const MAX_INTENT_BODY_BYTES = 4 * 1024;
const ALLOWED_FIELDS = new Set(["filename", "media_type", "byte_size", "sha256"]);
const ALLOWED_MEDIA_TYPES = new Set(["application/pdf", "image/png", "image/jpeg"]);
const ALLOWED_EXTENSIONS = [".pdf", ".png", ".jpg", ".jpeg"];
const PAYMENT_FIELD_PATTERN = /(pay|bank|erp|iban|swift|routing|account[_-]?number|transfer|wire|post[_-]?to)/i;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const PASSTHROUGH_STATUSES = new Set([401, 403, 404, 409, 410, 413, 415, 422, 500, 503]);

function isS3PostUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.username === "" && url.hostname.endsWith(".amazonaws.com");
  } catch {
    return false;
  }
}

async function gate(
  request: Request,
  deps: UploadDeps,
  requireJson: boolean,
): Promise<{ ok: true; config: ServerEnvConfig } | { ok: false; response: UploadResponse }> {
  const mode = deps.loadMode();
  if (!mode.valid) return { ok: false, response: failure(deps, 403, ["OPERATIONS_MODE_MISCONFIGURED"]) };
  if (mode.mode === "disabled") return { ok: false, response: failure(deps, 403, ["OPERATIONS_DISABLED"]) };
  if (loadUploadMode() !== "s3_direct") return { ok: false, response: failure(deps, 404, ["DIRECT_UPLOAD_UNAVAILABLE"]) };

  if (!isSameOriginRequest(request.headers)) return { ok: false, response: failure(deps, 403, ["CSRF_ORIGIN_REJECTED"]) };
  if (requireJson && !(request.headers.get("content-type") ?? "").toLowerCase().startsWith("application/json")) {
    return { ok: false, response: failure(deps, 415, ["UNSUPPORTED_MEDIA_TYPE"]) };
  }
  if (!deps.verifyCsrf(request.headers.get(CSRF_HEADER))) return { ok: false, response: failure(deps, 403, ["CSRF_TOKEN_INVALID"]) };

  try {
    return { ok: true, config: deps.loadEnv() };
  } catch (error) {
    if (error instanceof ServerConfigError) return { ok: false, response: failure(deps, 500, ["FRONTEND_AUTH_MISCONFIGURED"]) };
    throw error;
  }
}

export async function handleCreateUploadIntent(request: Request, deps: UploadDeps): Promise<UploadResponse> {
  const checked = await gate(request, deps, true);
  if (!checked.ok) return checked.response;
  const { config } = checked;

  const bytes = await readCappedBytes(request, MAX_INTENT_BODY_BYTES);
  if (bytes === null) return failure(deps, 413, ["REQUEST_TOO_LARGE"]);

  let body: unknown;
  try {
    body = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return failure(deps, 400, ["MALFORMED_JSON"]);
  }
  if (!isRecord(body)) return failure(deps, 400, ["MALFORMED_JSON"]);

  const names = Object.keys(body);
  if (names.some((name) => PAYMENT_FIELD_PATTERN.test(name))) return failure(deps, 422, ["PAYMENT_FIELD_PROHIBITED"]);
  if (names.some((name) => !ALLOWED_FIELDS.has(name))) return failure(deps, 422, ["FIELD_NOT_ALLOWED"]);

  const { filename, media_type: mediaType, byte_size: byteSize, sha256 } = body;
  if (typeof filename !== "string" || filename.length === 0 || filename.length > 128) return failure(deps, 422, ["FILENAME_INVALID"]);
  if (!ALLOWED_EXTENSIONS.some((extension) => filename.toLowerCase().endsWith(extension))) return failure(deps, 422, ["UNSUPPORTED_FILE_TYPE"]);
  if (typeof mediaType !== "string" || !ALLOWED_MEDIA_TYPES.has(mediaType)) return failure(deps, 415, ["MEDIA_TYPE_MISMATCH"]);
  if (typeof byteSize !== "number" || !Number.isInteger(byteSize) || byteSize <= 0) return failure(deps, 422, ["EMPTY_FILE"]);
  if (byteSize > MAX_FILE_BYTES) return failure(deps, 413, ["FILE_TOO_LARGE"]);
  if (typeof sha256 !== "string" || !SHA256_PATTERN.test(sha256)) return failure(deps, 422, ["SHA256_INVALID"]);

  const auth = await (deps.authHeaders ?? getBackendAuthHeaders)(config, deps.now());
  if (!auth.ok) {
    const authFailure = authFailureCode(auth.kind);
    return failure(deps, authFailure.status, [authFailure.code]);
  }

  const upstream = await callUpstream(deps, `${config.apiBaseUrl}/api/v1/operations/upload-intents`, {
    method: "POST",
    headers: { ...auth.headers, Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ filename, media_type: mediaType, byte_size: byteSize, sha256 }),
  });
  if (!upstream.ok) return failure(deps, 503, [upstream.code]);

  let upstreamJson: unknown;
  try {
    upstreamJson = await upstream.response.json();
  } catch {
    return failure(deps, 502, ["MALFORMED_RESPONSE"]);
  }

  if (upstream.response.status === 201) {
    const data = isRecord(upstreamJson) && isRecord(upstreamJson.data) ? upstreamJson.data : null;
    const fields = data !== null && isRecord(data.upload_fields) ? data.upload_fields : null;
    if (
      data === null ||
      fields === null ||
      typeof data.intent_id !== "string" ||
      !UUID_PATTERN.test(data.intent_id) ||
      !isS3PostUrl(data.upload_url) ||
      !Object.values(fields).every((value) => typeof value === "string") ||
      typeof data.maximum_bytes !== "number"
    ) {
      return failure(deps, 502, ["MALFORMED_RESPONSE"]);
    }
    return { status: 201, body: upstreamJson };
  }

  const problem = safeErrorCodes(upstreamJson);
  const status = PASSTHROUGH_STATUSES.has(upstream.response.status) ? upstream.response.status : 502;
  return failure(deps, status, problem.errors.length > 0 ? problem.errors : ["REQUEST_NOT_ACCEPTED"], problem.requestId);
}

export async function handleFinalizeUploadIntent(request: Request, intentId: string, deps: UploadDeps): Promise<UploadResponse> {
  if (!UUID_PATTERN.test(intentId)) return failure(deps, 404, ["UPLOAD_INTENT_NOT_FOUND"]);

  const checked = await gate(request, deps, false);
  if (!checked.ok) return checked.response;
  const { config } = checked;

  const auth = await (deps.authHeaders ?? getBackendAuthHeaders)(config, deps.now());
  if (!auth.ok) {
    const authFailure = authFailureCode(auth.kind);
    return failure(deps, authFailure.status, [authFailure.code]);
  }

  const upstream = await callUpstream(
    deps,
    `${config.apiBaseUrl}/api/v1/operations/upload-intents/${intentId.toLowerCase()}/finalize`,
    { method: "POST", headers: { ...auth.headers, Accept: "application/json" }, body: "" },
  );
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

import type { JobDetailPayload, JobListPayload, SubmissionPayload } from "@/types/api-payloads";

/** Browser-side calls. Everything goes through this application's own server boundary. */

export type ClientResult<T> = { ok: true; data: T; status: number } | { ok: false; errors: string[]; status: number | null };

function errorCodes(body: unknown): string[] {
  if (typeof body === "object" && body !== null && Array.isArray((body as { errors?: unknown }).errors)) {
    return (body as { errors: unknown[] }).errors.filter((code): code is string => typeof code === "string");
  }
  return [];
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

export async function submitInvoice(file: File, csrfToken: string, operationId: string): Promise<ClientResult<SubmissionPayload>> {
  const form = new FormData();
  form.set("operation_id", operationId);
  form.set("file", file, file.name);

  let response: Response;
  try {
    response = await fetch("/api/v1/operations/submissions", {
      method: "POST",
      headers: { "x-csrf-token": csrfToken },
      body: form,
      cache: "no-store",
    });
  } catch {
    return { ok: false, errors: ["NETWORK_ERROR"], status: null };
  }

  const body = await readJson(response);

  if ((response.status === 200 || response.status === 202) && typeof body === "object" && body !== null) {
    const data = (body as { data?: SubmissionPayload }).data;
    if (data && typeof data.job?.job_id === "string") return { ok: true, data, status: response.status };
    return { ok: false, errors: ["MALFORMED_RESPONSE"], status: response.status };
  }

  const codes = errorCodes(body);
  return { ok: false, errors: codes.length > 0 ? codes : ["INTERNAL_ERROR"], status: response.status };
}

export interface UploadIntentPayload {
  intent_id: string;
  upload_url: string;
  upload_fields: Record<string, string>;
  maximum_bytes: number;
}

async function sha256Hex(file: File): Promise<string | null> {
  if (typeof crypto === "undefined" || !crypto.subtle) return null;
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function postJson(url: string, csrfToken: string, body?: unknown): Promise<{ status: number | null; body: unknown }> {
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "x-csrf-token": csrfToken, ...(body === undefined ? {} : { "content-type": "application/json" }) },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
    return { status: response.status, body: await readJson(response) };
  } catch {
    return { status: null, body: null };
  }
}

/**
 * Staged direct upload (AWS mode): 1) ask this application for an upload intent (a short-lived presigned
 * POST), 2) send the file straight to S3 -- it never passes through the application server -- 3) ask this
 * application to finalize, which re-validates the stored object and queues the job.
 */
export async function submitInvoiceDirect(file: File, csrfToken: string): Promise<ClientResult<SubmissionPayload>> {
  const sha256 = await sha256Hex(file);
  if (sha256 === null) return { ok: false, errors: ["HASHING_UNSUPPORTED"], status: null };

  const mediaType = file.name.toLowerCase().endsWith(".pdf") ? "application/pdf" : file.name.toLowerCase().endsWith(".png") ? "image/png" : "image/jpeg";
  const intent = await postJson("/api/v1/operations/upload-intents", csrfToken, {
    filename: file.name,
    media_type: mediaType,
    byte_size: file.size,
    sha256,
  });
  const intentData = typeof intent.body === "object" && intent.body !== null ? (intent.body as { data?: UploadIntentPayload }).data : undefined;
  if (intent.status !== 201 || !intentData || typeof intentData.upload_url !== "string") {
    const codes = errorCodes(intent.body);
    return { ok: false, errors: codes.length > 0 ? codes : ["INTERNAL_ERROR"], status: intent.status };
  }

  // The policy fixes every field; the file part must come last.
  const form = new FormData();
  for (const [name, value] of Object.entries(intentData.upload_fields)) form.set(name, value);
  form.set("file", file, file.name);
  try {
    const stored = await fetch(intentData.upload_url, { method: "POST", body: form });
    if (!stored.ok) return { ok: false, errors: ["STORAGE_UPLOAD_FAILED"], status: stored.status };
  } catch {
    return { ok: false, errors: ["STORAGE_UPLOAD_FAILED"], status: null };
  }

  const finalized = await postJson(`/api/v1/operations/upload-intents/${intentData.intent_id}/finalize`, csrfToken);
  if ((finalized.status === 200 || finalized.status === 202) && typeof finalized.body === "object" && finalized.body !== null) {
    const data = (finalized.body as { data?: SubmissionPayload }).data;
    if (data && typeof data.job?.job_id === "string") return { ok: true, data, status: finalized.status };
    return { ok: false, errors: ["MALFORMED_RESPONSE"], status: finalized.status };
  }
  const codes = errorCodes(finalized.body);
  return { ok: false, errors: codes.length > 0 ? codes : ["INTERNAL_ERROR"], status: finalized.status };
}

export async function fetchJobList(options: { reviewCaseId?: string; pageSize?: number } = {}): Promise<ClientResult<JobListPayload>> {
  const params = new URLSearchParams({ page: "1", page_size: String(options.pageSize ?? 25) });
  if (options.reviewCaseId) params.set("review_case_id", options.reviewCaseId);
  try {
    const response = await fetch(`/api/backend/api/v1/operations/jobs?${params.toString()}`, { cache: "no-store" });
    const body = await readJson(response);
    if (response.ok && typeof body === "object" && body !== null && (body as { data?: unknown }).data) {
      return { ok: true, data: (body as { data: JobListPayload }).data, status: response.status };
    }
    return { ok: false, errors: errorCodes(body), status: response.status };
  } catch {
    return { ok: false, errors: ["NETWORK_ERROR"], status: null };
  }
}

export async function fetchJobDetail(jobId: string): Promise<ClientResult<JobDetailPayload>> {
  try {
    const response = await fetch(`/api/backend/api/v1/operations/jobs/${jobId}`, { cache: "no-store" });
    const body = await readJson(response);
    if (response.ok && typeof body === "object" && body !== null && (body as { data?: unknown }).data) {
      return { ok: true, data: (body as { data: JobDetailPayload }).data, status: response.status };
    }
    return { ok: false, errors: errorCodes(body), status: response.status };
  } catch {
    return { ok: false, errors: ["NETWORK_ERROR"], status: null };
  }
}

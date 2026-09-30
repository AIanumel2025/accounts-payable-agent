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

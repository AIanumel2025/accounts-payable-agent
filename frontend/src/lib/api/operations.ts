import "server-only";
import type { AppResult } from "@/lib/api/app-result";
import { withLoadedConfig } from "@/lib/api/app-result";
import { callBackend } from "@/lib/server/backend-request";
import type { JobDetailPayload, JobListPayload } from "@/types/api-payloads";

const UUID_PATTERN = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

export interface JobListFilters {
  page?: number;
  pageSize?: number;
  reviewCaseId?: string;
  jobType?: "PROCESS_DOCUMENT" | "RESUME_WORKFLOW";
}

/** `GET /api/v1/operations/jobs` -- tenant-scoped, server-side paginated. */
export function listJobs(filters: JobListFilters = {}): Promise<AppResult<JobListPayload>> {
  const searchParams: Record<string, string> = {
    page: String(filters.page ?? 1),
    page_size: String(filters.pageSize ?? 25),
  };
  if (filters.reviewCaseId) {
    if (!UUID_PATTERN.test(filters.reviewCaseId)) {
      return Promise.resolve({ ok: false, kind: "NOT_FOUND", message: "This review case could not be found." });
    }
    searchParams.review_case_id = filters.reviewCaseId;
  }
  if (filters.jobType) searchParams.job_type = filters.jobType;

  return withLoadedConfig(async (config) => {
    const result = await callBackend<JobListPayload>("/api/v1/operations/jobs", config, { searchParams });
    if (!result.ok) return { ok: false, kind: result.kind, message: result.message };
    return { ok: true, data: result.data };
  });
}

/** `GET /api/v1/operations/jobs/{id}` -- job with its event timeline. A malformed id is a local NOT_FOUND. */
export function getJobDetail(jobId: string): Promise<AppResult<JobDetailPayload>> {
  if (!UUID_PATTERN.test(jobId)) {
    return Promise.resolve({ ok: false, kind: "NOT_FOUND", message: "This job could not be found." });
  }

  return withLoadedConfig(async (config) => {
    const result = await callBackend<JobDetailPayload>(`/api/v1/operations/jobs/${jobId}`, config);
    if (!result.ok) return { ok: false, kind: result.kind, message: result.message };
    return { ok: true, data: result.data };
  });
}

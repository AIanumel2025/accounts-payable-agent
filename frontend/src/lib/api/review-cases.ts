import "server-only";
import type { AppResult } from "@/lib/api/app-result";
import { withLoadedConfig } from "@/lib/api/app-result";
import { filtersToBackendParams, type ReviewQueueFilters } from "@/lib/api/review-queue-query";
import { callBackend } from "@/lib/server/backend-request";
import type { InvoiceDetailPayload, ReviewQueuePagePayload } from "@/types/api-payloads";

const UUID_PATTERN = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

/** `GET /api/v1/review-cases` (M11B task §7). Server-driven pagination and filters -- never fetches the whole queue and filters it in memory. */
export function listReviewCases(filters: ReviewQueueFilters): Promise<AppResult<ReviewQueuePagePayload>> {
  return withLoadedConfig(async (config) => {
    const result = await callBackend<ReviewQueuePagePayload>("/api/v1/review-cases", config, {
      searchParams: filtersToBackendParams(filters),
    });
    if (!result.ok) return { ok: false, kind: result.kind, message: result.message };
    return { ok: true, data: result.data };
  });
}

/**
 * `GET /api/v1/review-cases/{review_case_id}` (M11B task §8). Validates
 * the id is a syntactically well-formed UUID before ever calling the
 * backend -- a malformed id (e.g. from a hand-edited URL) fails fast with
 * `NOT_FOUND` locally rather than sending a request FastAPI would reject
 * with `422` anyway, so the UI has one consistent "not found" path for
 * both a malformed id and a genuinely unknown/cross-tenant case
 * (`ap_agent.services.review_queries.get_invoice_detail` already returns
 * `404` for both, never distinguishing them -- task §14: "unknown cases
 * return a safe 404").
 */
export function getReviewCaseDetail(reviewCaseId: string): Promise<AppResult<InvoiceDetailPayload>> {
  if (!UUID_PATTERN.test(reviewCaseId)) {
    return Promise.resolve({ ok: false, kind: "NOT_FOUND", message: "This review case could not be found." });
  }

  return withLoadedConfig(async (config) => {
    const result = await callBackend<InvoiceDetailPayload>(`/api/v1/review-cases/${reviewCaseId}`, config);
    if (!result.ok) return { ok: false, kind: result.kind, message: result.message };
    return { ok: true, data: result.data };
  });
}

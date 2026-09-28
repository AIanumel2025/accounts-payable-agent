import type { ReviewCaseStatusValue, ReviewPriorityValue } from "@/types/api-payloads";

/**
 * Review-queue filter/pagination state and its URL/query-string
 * serialization (M11B task §7). Pure and framework-agnostic (no
 * `server-only`) so both the Server Component that reads
 * `searchParams` and the client-side filter controls that build the next
 * URL can share one implementation -- there is exactly one place that
 * knows the FastAPI query-parameter names.
 *
 * **Backend contract finding (task §4):** `GET /api/v1/review-cases`'s
 * `status` filter is declared `Optional[str]` in FastAPI
 * (`ap_agent/api/routes/review_cases.py`) but
 * `ap_agent.services.review_queries.list_review_queue` only recognizes
 * four of `ReviewCaseStatus`'s six members --
 * `_DATABASE_REVIEW_STATUSES_BY_CASE_STATUS` has no entry for
 * `AWAITING_INFORMATION`/`ESCALATED`. Passing either of those raises a
 * bare `ValueError` that (verified by reading `ap_agent/api/errors.py`)
 * is not one of the typed domain exceptions the central handlers map --
 * it falls through to the generic `Exception` handler as an
 * undifferentiated `500 INTERNAL_ERROR`, not a `422`. Independently, the
 * database's `review_status` column itself (`review_status_allowed`
 * CHECK constraint, `0002_operational_memory_tables.sql`) can only ever
 * hold `OPEN`/`CLAIMED`/`RESOLVED`/`CANCELLED`, which
 * `review_case_status_from_database`/`command_case_status_from_database`
 * map onto exactly `OPEN`/`IN_REVIEW`/`RESOLVED`/`REJECTED` -- so
 * `AWAITING_INFORMATION`/`ESCALATED` are not just unsupported as a
 * *filter* value, they are currently unreachable as a `case_status`
 * *value* anywhere in the system. `SUPPORTED_QUEUE_STATUS_FILTERS` below
 * is therefore the complete, exact, currently-usable set -- not an
 * arbitrary subset -- and the UI never offers the other two, so it can
 * never trigger that backend defect.
 */

export const SUPPORTED_QUEUE_STATUS_FILTERS: readonly ReviewCaseStatusValue[] = [
  "OPEN",
  "IN_REVIEW",
  "RESOLVED",
  "REJECTED",
];

export const SUPPORTED_QUEUE_PRIORITY_FILTERS: readonly ReviewPriorityValue[] = [
  "CRITICAL",
  "HIGH",
  "NORMAL",
  "LOW",
];

const UUID_PATTERN = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

export interface ReviewQueueFilters {
  page: number;
  pageSize?: number;
  status?: ReviewCaseStatusValue;
  assignedTo?: string;
  priority?: ReviewPriorityValue;
  batchId?: string;
}

export const DEFAULT_QUEUE_FILTERS: ReviewQueueFilters = { page: 1 };

export interface ParsedQueueFilters {
  filters: ReviewQueueFilters;
  /** Non-fatal: an individual malformed parameter was dropped rather than failing the whole request. */
  warnings: string[];
}

function isSupportedStatus(value: string): value is ReviewCaseStatusValue {
  return (SUPPORTED_QUEUE_STATUS_FILTERS as readonly string[]).includes(value);
}

function isSupportedPriority(value: string): value is ReviewPriorityValue {
  return (SUPPORTED_QUEUE_PRIORITY_FILTERS as readonly string[]).includes(value);
}

/**
 * Parses a review-queue URL's search params into validated filters.
 * Never throws: a malformed individual parameter (invalid page number,
 * unsupported status, malformed batch UUID) is dropped with a warning
 * rather than failing the whole page (task §7: "malformed URL
 * parameters", "invalid batch UUID").
 */
export function parseQueueFiltersFromSearchParams(searchParams: URLSearchParams): ParsedQueueFilters {
  const warnings: string[] = [];
  const filters: ReviewQueueFilters = { page: 1 };

  const rawPage = searchParams.get("page");
  if (rawPage !== null) {
    const parsedPage = Number.parseInt(rawPage, 10);
    if (Number.isInteger(parsedPage) && parsedPage >= 1 && String(parsedPage) === rawPage) {
      filters.page = parsedPage;
    } else {
      warnings.push(`Ignored invalid page value: ${rawPage}`);
    }
  }

  const rawPageSize = searchParams.get("page_size");
  if (rawPageSize !== null) {
    const parsedPageSize = Number.parseInt(rawPageSize, 10);
    if (Number.isInteger(parsedPageSize) && parsedPageSize >= 1 && String(parsedPageSize) === rawPageSize) {
      filters.pageSize = parsedPageSize;
    } else {
      warnings.push(`Ignored invalid page_size value: ${rawPageSize}`);
    }
  }

  const rawStatus = searchParams.get("status");
  if (rawStatus !== null) {
    if (isSupportedStatus(rawStatus)) {
      filters.status = rawStatus;
    } else {
      warnings.push(`Ignored unsupported status filter: ${rawStatus}`);
    }
  }

  const rawAssignedTo = searchParams.get("assigned_to");
  if (rawAssignedTo !== null && rawAssignedTo.trim() !== "") {
    filters.assignedTo = rawAssignedTo;
  }

  const rawPriority = searchParams.get("priority");
  if (rawPriority !== null) {
    if (isSupportedPriority(rawPriority)) {
      filters.priority = rawPriority;
    } else {
      warnings.push(`Ignored unsupported priority filter: ${rawPriority}`);
    }
  }

  const rawBatchId = searchParams.get("batch_id");
  if (rawBatchId !== null) {
    if (UUID_PATTERN.test(rawBatchId)) {
      filters.batchId = rawBatchId;
    } else {
      warnings.push(`Ignored invalid batch_id (not a UUID): ${rawBatchId}`);
    }
  }

  return { filters, warnings };
}

/** Builds the exact FastAPI query-parameter record for `GET /api/v1/review-cases`. */
export function filtersToBackendParams(filters: ReviewQueueFilters): Record<string, string> {
  const params: Record<string, string> = { page: String(filters.page) };
  if (filters.pageSize !== undefined) params.page_size = String(filters.pageSize);
  if (filters.status !== undefined) params.status = filters.status;
  if (filters.assignedTo !== undefined) params.assigned_to = filters.assignedTo;
  if (filters.priority !== undefined) params.priority = filters.priority;
  if (filters.batchId !== undefined) params.batch_id = filters.batchId;
  return params;
}

/** Builds the URL search params for a link to the queue with these filters (used by both the filter UI and dashboard deep links). */
export function filtersToUrlSearchParams(filters: ReviewQueueFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.page !== 1) params.set("page", String(filters.page));
  if (filters.pageSize !== undefined) params.set("page_size", String(filters.pageSize));
  if (filters.status !== undefined) params.set("status", filters.status);
  if (filters.assignedTo !== undefined) params.set("assigned_to", filters.assignedTo);
  if (filters.priority !== undefined) params.set("priority", filters.priority);
  if (filters.batchId !== undefined) params.set("batch_id", filters.batchId);
  return params;
}

export function hasActiveFilters(filters: ReviewQueueFilters): boolean {
  return (
    filters.status !== undefined ||
    filters.assignedTo !== undefined ||
    filters.priority !== undefined ||
    filters.batchId !== undefined
  );
}

export function clearedFilters(filters: ReviewQueueFilters): ReviewQueueFilters {
  return { page: 1, pageSize: filters.pageSize };
}

/**
 * Validates a detail page's `?from=` breadcrumb-return value (M11B task
 * §2/§8). Only ever used to build a same-app `<Link href>` -- so it must be
 * a path-relative URL under `/review-queue`, never an absolute or
 * protocol-relative one (which could otherwise turn a shared/bookmarked
 * detail-page link into an open redirect). Falls back to the plain queue
 * route on anything else, exactly like a malformed queue filter is dropped
 * rather than failing the page.
 */
export function parseSafeReturnTo(value: string | string[] | undefined): string {
  const FALLBACK = "/review-queue";
  if (typeof value !== "string" || value.length === 0) return FALLBACK;
  if (!value.startsWith("/review-queue")) return FALLBACK;
  if (value.startsWith("//") || value.includes("\\")) return FALLBACK;
  return value;
}

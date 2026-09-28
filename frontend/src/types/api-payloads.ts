import type { components } from "@/types/api.generated";

/**
 * Narrow types for `ApiEnvelope.data` (M11A task §4).
 *
 * The M10 FastAPI routes declare `response_model=ApiEnvelope`, whose `data`
 * field is typed `Optional[Any]` in Pydantic (`src/ap_agent/api/schemas.py`)
 * -- each route builds its actual payload with an explicit `from_domain`
 * classmethod and calls `.model_dump(mode="json")` on it, but that payload
 * type never appears in the OpenAPI schema itself, so `openapi-typescript`
 * can only generate `data?: unknown | null` for every endpoint (verified in
 * `src/types/api.generated.ts`).
 *
 * These payload shapes are therefore hand-written from
 * `src/ap_agent/api/schemas.py`'s `DashboardResponse`/`ReviewReasonCount`
 * and `src/ap_agent/api/routes/health.py`'s inline health payload, and must
 * be kept in sync by hand if those backend shapes change -- `api:check`
 * cannot catch drift in this file because the backend does not publish it.
 * This is a documented limitation, not a silent gap (CLAUDE.md).
 */

export type ApiEnvelope = components["schemas"]["ApiEnvelope"];

export interface ReviewReasonCountPayload {
  reason: string;
  count: number;
}

export interface DashboardPayload {
  tenant_id: string;
  generated_at: string;
  total_invoices: number;
  processing_invoices: number;
  completed_invoices: number;
  review_required_invoices: number;
  failed_invoices: number;
  open_review_cases: number;
  unassigned_review_cases: number;
  review_reason_counts: ReviewReasonCountPayload[];
}

export type HealthCommandMode = "COMMIT" | "VALIDATION_ONLY";

export interface HealthPayload {
  service: string;
  api_version: string;
  command_mode: HealthCommandMode;
  payment_execution: "PROHIBITED";
}

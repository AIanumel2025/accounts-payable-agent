import type { components } from "@/types/api.generated";

/**
 * Typed aliases for `ApiEnvelope.data` per endpoint (M11B task §4).
 *
 * M11A found `ApiEnvelope.data` typed `Optional[Any]` in Pydantic
 * (`src/ap_agent/api/schemas.py`), so `openapi-typescript` could only
 * generate `data?: unknown` for every endpoint, and worked around it with
 * hand-maintained shadow interfaces in this file -- something `api:check`
 * could not catch drift in.
 *
 * M11B fixed the root cause instead of extending the workaround:
 * `ApiEnvelope` is now `Generic[T]` in `schemas.py`, and every read route
 * declares `response_model=ApiEnvelope[SomeResponse]` (verified to produce
 * byte-identical runtime JSON to before -- see
 * `docs/m11b_review_queue_detail_report.md`, "OpenAPI envelope decision").
 * FastAPI now emits a distinct, fully-typed `ApiEnvelope_SomeResponse_`
 * schema per endpoint, so every payload type below is a plain alias into
 * the generated file -- no hand-maintained shape, no drift `api:check`
 * cannot catch.
 */

export type DashboardPayload = components["schemas"]["DashboardResponse"];
export type ReviewReasonCountPayload = components["schemas"]["ReviewReasonCount"];

export type HealthPayload = components["schemas"]["HealthResponse"];
export type HealthCommandMode = HealthPayload["command_mode"];

export type ReviewQueuePagePayload = components["schemas"]["ReviewQueuePageResponse"];
export type ReviewQueueItemPayload = components["schemas"]["ReviewQueueItem"];
export type PaginationMetaPayload = components["schemas"]["PaginationMeta"];

export type InvoiceDetailPayload = components["schemas"]["InvoiceDetailResponse"];
export type InterfaceFieldValuePayload = components["schemas"]["InterfaceFieldValueResponse"];
export type FinancialCheckPayload = components["schemas"]["FinancialCheckResponse"];
export type LineMatchPayload = components["schemas"]["LineMatchResponse"];
export type TimelineEventPayload = components["schemas"]["TimelineEventResponse"];
export type ReviewDecisionPayload = components["schemas"]["ReviewDecisionResponse"];

export type ReviewCaseStatusValue = components["schemas"]["ReviewCaseStatus"];
export type ReviewPriorityValue = components["schemas"]["ReviewPriority"];
export type InvoiceWorkflowStatusValue = components["schemas"]["InvoiceWorkflowStatus"];
export type OrchestrationStageValue = components["schemas"]["OrchestrationStage"];
export type InvoiceFieldNameValue = components["schemas"]["InvoiceFieldName"];
export type NormalizedValueTypeValue = components["schemas"]["NormalizedValueType"];
export type HumanReviewDispositionValue = components["schemas"]["HumanReviewDisposition"];

// M11C: typed command + capability payloads (all aliases into the generated
// contract; `api:check` therefore detects command-contract drift).
export type CommandCapabilitiesPayload = components["schemas"]["CommandCapabilitiesResponse"];
export type AvailableActionPayload = components["schemas"]["AvailableAction"];
export type CorrectionPolicyPayload = components["schemas"]["CorrectionPolicyResponse"];
export type ResumeCapabilityPayload = components["schemas"]["ResumeCapabilityResponse"];
export type CorrectableHeaderFieldPayload = components["schemas"]["CorrectableHeaderField"];
export type CorrectableLineValuePayload = components["schemas"]["CorrectableLineValue"];

export type ApiCommandRequest = components["schemas"]["ApiReviewCommandRequest"];
export type ApiCorrectionRequestPayload = components["schemas"]["ApiCorrectionRequest"];
export type ValidationOnlyCommandPayload = components["schemas"]["ValidationOnlyCommandResponse"];
export type CommittedCommandPayload = components["schemas"]["CommandResultResponse"];
export type WorkflowResumePayload = components["schemas"]["WorkflowResumeResponse"];
export type ApiErrorEnvelopePayload = components["schemas"]["ApiErrorEnvelope"];
export type ReviewActionValue = components["schemas"]["ReviewAction"];

// M11D Core: operations console (aliases into the generated contract).
export type HealthOperationsMode = HealthPayload["operations_mode"];
export type JobPayload = components["schemas"]["JobResponse"];
export type JobSummaryPayload = components["schemas"]["JobSummaryResponse"];
export type JobEventPayload = components["schemas"]["JobEventResponse"];
export type JobDetailPayload = components["schemas"]["JobDetailResponse"];
export type JobListPayload = components["schemas"]["JobListResponse"];
export type SubmissionPayload = components["schemas"]["SubmissionResponse"];
export type JobStatusValue = JobPayload["status"];
export type JobTypeValue = JobPayload["job_type"];

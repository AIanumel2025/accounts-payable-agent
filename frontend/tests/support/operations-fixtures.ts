import type { JobDetailPayload, JobPayload } from "@/types/api-payloads";

export const JOB_ID = "11111111-1111-4111-8111-111111111111";
export const CASE_ID = "22222222-2222-4222-8222-222222222222";
export const NEW_CASE_ID = "33333333-3333-4333-8333-333333333333";

export function makeJob(over: Partial<JobPayload> = {}): JobPayload {
  return {
    job_id: JOB_ID,
    job_type: "PROCESS_DOCUMENT",
    status: "QUEUED",
    current_stage: null,
    source_name: "invoice-001.pdf",
    media_type: "application/pdf",
    byte_size: 1234,
    attempt_count: 0,
    error_code: null,
    review_case_id: null,
    resumed_review_case_id: null,
    workflow_id: null,
    summary: { review_reasons: [], stages: [], executed_stages: [], corrected_fields: [] },
    created_at: "2026-06-15T10:00:00Z",
    started_at: null,
    completed_at: null,
    updated_at: "2026-06-15T10:00:00Z",
    ...over,
  } as JobPayload;
}

export function makeDetail(jobOver: Partial<JobPayload> = {}, events: JobDetailPayload["events"] = []): JobDetailPayload {
  return { job: makeJob(jobOver), events };
}

export function event(sequence: number, eventType: string, stage: string | null, message = "ok", status = "RUNNING") {
  return { sequence_number: sequence, event_type: eventType, stage, status, attempt_number: 1, message, error_code: null, occurred_at: "2026-06-15T10:00:0" + (sequence % 10) + "Z" };
}

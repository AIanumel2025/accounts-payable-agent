/**
 * Status label + tone mapping (M11A task §11/§12). Every status is paired
 * with a human-readable label and a short glyph -- colour (`tone`) is
 * never the only signal (task §7/§12: "Status must never be communicated
 * by colour alone.").
 */

export type StatusTone = "success" | "warning" | "review-required" | "failed" | "neutral";

export interface StatusPresentation {
  label: string;
  tone: StatusTone;
  glyph: string;
}

const WORKFLOW_STATUS_MAP: Record<string, StatusPresentation> = {
  PENDING: { label: "Pending", tone: "neutral", glyph: "○" },
  IN_PROGRESS: { label: "Processing", tone: "neutral", glyph: "◐" },
  SUCCEEDED: { label: "Completed automatically", tone: "success", glyph: "✓" },
  REVIEW_REQUIRED: { label: "Review required", tone: "review-required", glyph: "!" },
  FAILED: { label: "Failed", tone: "failed", glyph: "✕" },
  SKIPPED: { label: "Skipped", tone: "neutral", glyph: "○" },
};

const REVIEW_CASE_STATUS_MAP: Record<string, StatusPresentation> = {
  OPEN: { label: "Open", tone: "review-required", glyph: "!" },
  IN_REVIEW: { label: "In review", tone: "warning", glyph: "◐" },
  AWAITING_INFORMATION: { label: "Awaiting information", tone: "warning", glyph: "…" },
  ESCALATED: { label: "Escalated", tone: "failed", glyph: "▲" },
  RESOLVED: { label: "Resolved", tone: "success", glyph: "✓" },
  REJECTED: { label: "Rejected", tone: "failed", glyph: "✕" },
};

const BACKEND_HEALTH_MAP: Record<string, StatusPresentation> = {
  HEALTHY: { label: "Backend connected", tone: "success", glyph: "✓" },
  UNAVAILABLE: { label: "Backend unavailable", tone: "failed", glyph: "✕" },
  DEGRADED: { label: "Backend degraded", tone: "warning", glyph: "!" },
};

/**
 * Stage labels (M11B task §8A/§8G). Covers every `OrchestrationStage`
 * member (`src/ap_agent/models/orchestration.py`, used by
 * `InvoiceDetailResponse.current_stage`) plus `REASONING`, the one
 * `MemoryWorkflowStage` member (`src/ap_agent/models/memory.py`) that
 * `OrchestrationStage` doesn't have -- `TimelineEventResponse.stage`
 * (`src/ap_agent/api/schemas.py`) is sourced from audit-event rows built
 * from `MemoryWorkflowStage`, a distinct enum, so this map is the union of
 * both rather than assuming they're identical.
 */
const STAGE_MAP: Record<string, StatusPresentation> = {
  INGESTION: { label: "Ingestion", tone: "neutral", glyph: "○" },
  PREPROCESSING: { label: "Preprocessing", tone: "neutral", glyph: "○" },
  OCR: { label: "OCR", tone: "neutral", glyph: "○" },
  NORMALIZATION: { label: "Normalization", tone: "neutral", glyph: "○" },
  FINANCIAL_VALIDATION: { label: "Financial validation", tone: "neutral", glyph: "○" },
  REFERENCE_MATCHING: { label: "Reference matching", tone: "neutral", glyph: "○" },
  MEMORY_PERSISTENCE: { label: "Memory persistence", tone: "neutral", glyph: "○" },
  REASONING: { label: "Reasoning", tone: "neutral", glyph: "○" },
  HUMAN_REVIEW: { label: "Human review", tone: "review-required", glyph: "!" },
  COMPLETED: { label: "Completed", tone: "success", glyph: "✓" },
};

const PRIORITY_MAP: Record<string, StatusPresentation> = {
  CRITICAL: { label: "Critical", tone: "failed", glyph: "▲" },
  HIGH: { label: "High", tone: "review-required", glyph: "▲" },
  NORMAL: { label: "Normal", tone: "neutral", glyph: "●" },
  LOW: { label: "Low", tone: "neutral", glyph: "▽" },
};

/**
 * Financial-check and line-match statuses (M11B task §8D/§8F). These are
 * plain `str` fields in the FastAPI schema
 * (`FinancialCheckResponse.status`, `LineMatchResponse.*_status`), not a
 * typed backend enum -- verified by reading
 * `src/ap_agent/api/schemas.py`. This map covers every value observed in
 * the real Phase 1-8 golden results
 * (`tests/golden/phase_5_expected_results.json`'s
 * `expected_summary`/`expected_header_checks`); an unrecognized future
 * value still renders via the humanized fallback, never a crash (task
 * §12: "unknown future values render a controlled fallback").
 */
const VALIDATION_STATUS_MAP: Record<string, StatusPresentation> = {
  PASSED: { label: "Passed", tone: "success", glyph: "✓" },
  FAILED: { label: "Failed", tone: "failed", glyph: "✕" },
  REVIEW_REQUIRED: { label: "Review required", tone: "review-required", glyph: "!" },
  SKIPPED: { label: "Not evaluated", tone: "neutral", glyph: "○" },
  NOT_APPLICABLE: { label: "Not applicable", tone: "neutral", glyph: "○" },
};

const MATCH_STATUS_MAP: Record<string, StatusPresentation> = {
  MATCHED: { label: "Matched", tone: "success", glyph: "✓" },
  NOT_MATCHED: { label: "Not matched", tone: "failed", glyph: "✕" },
  REVIEW_REQUIRED: { label: "Review required", tone: "review-required", glyph: "!" },
  NOT_REFERENCED: { label: "Not referenced", tone: "neutral", glyph: "○" },
  NOT_FOUND: { label: "Not found", tone: "warning", glyph: "?" },
};

const DISPOSITION_MAP: Record<string, StatusPresentation> = {
  APPROVED: { label: "Approved", tone: "success", glyph: "✓" },
  REJECTED: { label: "Rejected", tone: "failed", glyph: "✕" },
  HOLD: { label: "On hold", tone: "warning", glyph: "…" },
  NEEDS_INFORMATION: { label: "Needs information", tone: "warning", glyph: "?" },
  CORRECTED: { label: "Corrected", tone: "success", glyph: "✓" },
};

function lookup(map: Record<string, StatusPresentation>, code: string | null | undefined): StatusPresentation {
  if (!code) return { label: "Unknown", tone: "neutral", glyph: "?" };
  return map[code] ?? { label: humanizeCode(code), tone: "neutral", glyph: "?" };
}

export function humanizeCode(code: string): string {
  return code
    .toLowerCase()
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export function workflowStatusLabel(code: string | null | undefined): StatusPresentation {
  return lookup(WORKFLOW_STATUS_MAP, code);
}

export function reviewCaseStatusLabel(code: string | null | undefined): StatusPresentation {
  return lookup(REVIEW_CASE_STATUS_MAP, code);
}

export function backendHealthLabel(code: string | null | undefined): StatusPresentation {
  return lookup(BACKEND_HEALTH_MAP, code);
}

export function priorityLabel(code: string | null | undefined): StatusPresentation {
  return lookup(PRIORITY_MAP, code);
}

export function stageLabel(code: string | null | undefined): StatusPresentation {
  return lookup(STAGE_MAP, code);
}

export function validationStatusLabel(code: string | null | undefined): StatusPresentation {
  return lookup(VALIDATION_STATUS_MAP, code);
}

export function matchStatusLabel(code: string | null | undefined): StatusPresentation {
  return lookup(MATCH_STATUS_MAP, code);
}

export function dispositionLabel(code: string | null | undefined): StatusPresentation {
  return lookup(DISPOSITION_MAP, code);
}

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

function lookup(map: Record<string, StatusPresentation>, code: string | null | undefined): StatusPresentation {
  if (!code) return { label: "Unknown", tone: "neutral", glyph: "?" };
  return map[code] ?? { label: humanizeCode(code), tone: "neutral", glyph: "?" };
}

function humanizeCode(code: string): string {
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

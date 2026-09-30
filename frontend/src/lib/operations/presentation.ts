import type { StatusPresentation } from "@/lib/formatting/status";
import type { JobPayload } from "@/types/api-payloads";

/**
 * Pure presentation helpers for the operations console (M11D Core).
 * Every status is a label + glyph + tone (never colour alone); every error
 * code maps to a fixed sentence -- an unknown code falls back to a generic
 * message, never to the raw code text.
 */

export const TERMINAL_JOB_STATUSES: ReadonlySet<string> = new Set(["SUCCEEDED", "REVIEW_REQUIRED", "FAILED"]);

export function isTerminalJob(job: Pick<JobPayload, "status">): boolean {
  return TERMINAL_JOB_STATUSES.has(job.status);
}

export function jobTypeLabel(jobType: string): string {
  return jobType === "RESUME_WORKFLOW" ? "Workflow resume" : "Invoice processing";
}

export function jobStatusPresentation(job: Pick<JobPayload, "status" | "job_type">): StatusPresentation {
  const isResume = job.job_type === "RESUME_WORKFLOW";
  switch (job.status) {
    case "QUEUED":
      return { label: "Queued", tone: "neutral", glyph: "○" };
    case "RUNNING":
      return { label: "Running", tone: "warning", glyph: "◐" };
    case "SUCCEEDED":
      return { label: isResume ? "Completed" : "Completed automatically", tone: "success", glyph: "✓" };
    case "REVIEW_REQUIRED":
      return { label: isResume ? "Returned to review" : "Review required", tone: "review-required", glyph: "!" };
    case "FAILED":
      return { label: "Failed", tone: "failed", glyph: "✕" };
    default:
      return { label: "Unknown", tone: "neutral", glyph: "?" };
  }
}

/** One-sentence outcome for list rows and headlines. Never claims success before the worker says so. */
export function jobOutcomeText(job: JobPayload): string {
  const isResume = job.job_type === "RESUME_WORKFLOW";
  switch (job.status) {
    case "QUEUED":
      return isResume ? "Waiting for the worker to resume the workflow." : "Waiting for the worker to start processing.";
    case "RUNNING":
      return "The worker is processing this job.";
    case "SUCCEEDED":
      return isResume
        ? "Resumed from the recorded restart stage and completed."
        : "Processed and completed automatically; no review was needed.";
    case "REVIEW_REQUIRED":
      return isResume
        ? "Resumed, but downstream checks still require review. A new review case was opened."
        : "Processed; the invoice needs human review.";
    default:
      return "Processing stopped. See the failure details.";
  }
}

interface ErrorCopy {
  title: string;
  description: string;
  nextAction: string;
}

const JOB_ERRORS: Record<string, ErrorCopy> = {
  ARTIFACT_HASH_MISMATCH: { title: "Stored upload failed verification", description: "The stored file no longer matches the fingerprint recorded at upload, so nothing was processed.", nextAction: "Upload the invoice again." },
  ARTIFACT_MISSING: { title: "Stored upload is missing", description: "The uploaded file could not be found by the worker.", nextAction: "Upload the invoice again." },
  REVIEW_CASE_MISSING: { title: "Review case missing", description: "Review was required but no review case could be found.", nextAction: "Contact an administrator." },
  DUPLICATE_DOCUMENT: { title: "Duplicate document", description: "This document was already processed.", nextAction: "Open the existing result instead." },
  WORKER_INTERNAL_ERROR: { title: "The worker hit an unexpected error", description: "Processing stopped before it finished. No result was recorded as successful.", nextAction: "Submit again, or contact an administrator if it repeats." },
  RESUME_ORIGINAL_MEMORY_HASH_MISMATCH: { title: "Stored invoice data failed verification", description: "The stored invoice record no longer matches its recorded hash, so the resume was refused before any stage ran.", nextAction: "Contact an administrator; do not retry." },
  RESUME_SOURCE_MEMORY_HASH_MISMATCH: { title: "Stored invoice data failed verification", description: "The stored invoice record no longer matches its recorded hash, so the resume was refused before any stage ran.", nextAction: "Contact an administrator; do not retry." },
  RESUME_OVERLAY_HASH_MISMATCH: { title: "Stored correction failed verification", description: "The recorded correction does not match its fingerprint, so the resume was refused before any stage ran.", nextAction: "Contact an administrator; do not retry." },
  RESUME_OVERLAY_TAMPERED: { title: "Stored correction failed verification", description: "The recorded correction differs from the reviewer's decision, so the resume was refused.", nextAction: "Contact an administrator; do not retry." },
  RESUME_NORMALIZATION_HASH_MISMATCH: { title: "Stored invoice data failed verification", description: "The normalized invoice no longer matches the resume plan, so the resume was refused.", nextAction: "Contact an administrator; do not retry." },
  RESUME_PLAN_NOT_FOUND: { title: "Resume plan not found", description: "The recorded resume plan could not be verified.", nextAction: "Contact an administrator." },
  RESUME_PLAN_IDENTITY_MISMATCH: { title: "Resume plan failed verification", description: "The resume plan identity does not match its decision.", nextAction: "Contact an administrator; do not retry." },
};

const GENERIC_JOB_ERROR: ErrorCopy = {
  title: "Processing failed",
  description: "The worker stopped this job at a controlled failure. No result was recorded as successful.",
  nextAction: "Review the timeline, then submit again or contact an administrator.",
};

export function jobErrorPresentation(code: string | null | undefined): ErrorCopy {
  if (!code) return GENERIC_JOB_ERROR;
  if (JOB_ERRORS[code]) return JOB_ERRORS[code]!;
  if (/_(TRANSIENT|PERMANENT|INTEGRITY|POLICY|UNKNOWN)_FAILURE$/.test(code)) {
    return { ...GENERIC_JOB_ERROR, description: "A pipeline stage failed and the worker stopped the job. No result was recorded as successful." };
  }
  return GENERIC_JOB_ERROR;
}

const UPLOAD_ERRORS: Record<string, string> = {
  OPERATIONS_DISABLED: "Invoice processing is disabled on this deployment.",
  OPERATIONS_MODE_MISCONFIGURED: "The operations setting is invalid, so uploads are disabled.",
  OPERATIONS_MODE_MISMATCH: "The frontend and backend are configured for different operations modes, so nothing was sent. Ask an administrator to align them.",
  CSRF_ORIGIN_REJECTED: "The request did not come from this application. Reload the page and try again.",
  CSRF_TOKEN_INVALID: "The page's security token is missing or expired. Reload the page and try again.",
  UNSUPPORTED_MEDIA_TYPE: "The upload was not sent as a file form.",
  REQUEST_TOO_LARGE: "The file is too large. The limit is 10 MB.",
  FILE_TOO_LARGE: "The file is too large. The limit is 10 MB.",
  EMPTY_FILE: "The file is empty.",
  UNSUPPORTED_FILE_TYPE: "Only PDF, PNG and JPEG invoices are supported.",
  FILE_SIGNATURE_MISMATCH: "The file contents do not match its extension.",
  MEDIA_TYPE_MISMATCH: "The file type does not match its extension.",
  MALFORMED_FILE: "The file looks damaged or incomplete.",
  FILENAME_INVALID: "The file name contains characters that are not allowed.",
  FILENAME_TOO_LONG: "The file name is too long (128 characters at most).",
  FILE_REQUIRED: "Choose one invoice file to upload.",
  FIELD_NOT_ALLOWED: "The request contained an unexpected field, so nothing was sent.",
  PAYMENT_FIELD_PROHIBITED: "Payment, bank and ERP fields do not exist in this system.",
  OPERATION_ID_INVALID: "The request identity was invalid. Reload the page and try again.",
  IDEMPOTENCY_KEY_CONTENT_CONFLICT: "This request identity was already used for a different file. Reload the page and try again.",
  ACTION_NOT_PERMITTED: "Your role is not permitted to submit invoices.",
  FRONTEND_AUTH_MISCONFIGURED: "The server-side connection to the API is not configured correctly.",
  DATABASE_UNAVAILABLE: "The database is temporarily unavailable. Nothing was confirmed; try again in a moment.",
  BACKEND_UNAVAILABLE: "The API could not be reached. Nothing was confirmed; try again.",
  BACKEND_TIMEOUT: "The API did not respond in time. The upload may not have been recorded; check Recent jobs before retrying.",
  NETWORK_ERROR: "The upload could not reach the server. You can retry.",
  MALFORMED_RESPONSE: "The backend returned a response this application could not understand. Check Recent jobs before retrying.",
  MALFORMED_MULTIPART: "The upload could not be read.",
  INTERNAL_ERROR: "An unexpected error occurred. Check Recent jobs before retrying.",
};

export function uploadErrorMessages(codes: readonly string[]): string[] {
  const messages = [...new Set(codes.map((code) => UPLOAD_ERRORS[code.split(":")[0] ?? code]).filter((m): m is string => Boolean(m)))];
  return messages.length > 0 ? messages : ["The upload was not accepted."];
}

const EVENT_LABELS: Record<string, string> = {
  JOB_SUBMITTED: "Queued",
  JOB_CLAIMED: "Worker started",
  ARTIFACT_VERIFIED: "Upload verified",
  RESUME_INPUTS_VERIFIED: "Stored hashes verified",
  WORKFLOW_STARTED: "Pipeline started",
  WORKFLOW_RESUMED: "Workflow resumed",
  STAGE_STARTED: "Stage started",
  STAGE_COMPLETED: "Stage completed",
  STAGE_RETRY_SCHEDULED: "Stage retry scheduled",
  STAGE_FAILED: "Stage failed",
  REVIEW_ROUTED: "Routed to review",
  WORKFLOW_COMPLETED: "Pipeline completed",
  WORKFLOW_FAILED: "Pipeline failed",
  JOB_SUCCEEDED: "Completed",
  JOB_REVIEW_REQUIRED: "Review required",
  JOB_FAILED: "Failed",
};

export function jobEventLabel(eventType: string): string {
  return EVENT_LABELS[eventType] ?? "Event";
}

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const ALLOWED_EXTENSIONS = [".pdf", ".png", ".jpg", ".jpeg"];

/** Fast client-side check; the server re-validates everything authoritatively. */
export function validateSelectedFile(file: { name: string; size: number }): string | null {
  if (file.size === 0) return "The file is empty.";
  if (file.size > MAX_UPLOAD_BYTES) return "The file is too large. The limit is 10 MB.";
  const lower = file.name.toLowerCase();
  if (!ALLOWED_EXTENSIONS.some((extension) => lower.endsWith(extension))) return "Only PDF, PNG and JPEG invoices are supported.";
  return null;
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

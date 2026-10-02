/**
 * Maps controlled backend/boundary error codes to user-facing copy (M11C
 * task §18). Every string is safe to render: no SQL, DSN, hostname, stack
 * trace or local path is ever produced here -- unknown codes fall back to a
 * generic sentence, never to the raw code text.
 */

export interface ErrorPresentation {
  title: string;
  description: string;
  /** A conflict the user must resolve by reloading current data; never silently retried. */
  needsRefresh: boolean;
}

const M: Record<string, { title: string; description: string; needsRefresh?: boolean }> = {
  STALE_REVIEW_REVISION: { title: "This case changed", description: "The case was updated after you loaded it. Refresh to review the current data, then try again.", needsRefresh: true },
  STALE_WORKFLOW_REVISION: { title: "This case changed", description: "The workflow moved on after you loaded this page. Refresh to review the current data, then try again.", needsRefresh: true },
  CASE_NOT_OPEN: { title: "Case is no longer open", description: "Another session already claimed or resolved this case. Refresh to see its current state.", needsRefresh: true },
  CASE_ALREADY_ASSIGNED: { title: "Already claimed", description: "Another reviewer claimed this case first. Nothing was changed.", needsRefresh: true },
  CASE_NOT_CLAIMED: { title: "Case is not claimed", description: "You must hold the claim on this case to do that. Refresh to see its current state.", needsRefresh: true },
  CASE_ASSIGNED_TO_DIFFERENT_REVIEWER: { title: "Owned by another reviewer", description: "This case is claimed by a different reviewer, so you cannot act on it.", needsRefresh: true },
  DECISION_ACTOR_MISMATCH: { title: "Only the deciding reviewer can do this", description: "Workflow resume can only be requested by the reviewer who recorded the decision.", needsRefresh: true },
  IDEMPOTENCY_KEY_CONTENT_CONFLICT: { title: "Conflicting duplicate request", description: "This request identity was already used for different content. Nothing was changed; reload and try again.", needsRefresh: true },
  WORKFLOW_NOT_AWAITING_RESUME: { title: "Resume already requested", description: "A workflow resume handoff was already created for this case. Nothing was changed.", needsRefresh: true },
  ACTION_NOT_PERMITTED: { title: "Not permitted", description: "Your role is not permitted to perform this action.", needsRefresh: false },
  CROSS_TENANT_COMMAND: { title: "Not permitted", description: "This action is not permitted for this tenant.", needsRefresh: false },
  REASON_CODE_REQUIRED: { title: "Reason required", description: "Select at least one reason code.", needsRefresh: false },
  NOTES_REQUIRED: { title: "Notes required", description: "Rejecting a case requires notes explaining why.", needsRefresh: false },
  CORRECTIONS_REQUIRED: { title: "Correction required", description: "Add at least one correction.", needsRefresh: false },
  CORRECTIONS_NOT_ALLOWED: { title: "Corrections not allowed", description: "Only a correction may include corrected values.", needsRefresh: false },
  CORRECTION_REASON_REQUIRED: { title: "Correction reason required", description: "Every correction needs a meaningful reason.", needsRefresh: false },
  CORRECTION_EVIDENCE_REQUIRED: { title: "Evidence required", description: "Every correction needs at least one supporting evidence reference.", needsRefresh: false },
  UNKNOWN_EVIDENCE_REFERENCE: { title: "Unknown evidence", description: "A selected evidence reference does not belong to this case.", needsRefresh: true },
  UNKNOWN_INVOICE_LINE_NUMBER: { title: "Unknown line", description: "A correction targets an invoice line that does not exist.", needsRefresh: true },
  PREVIOUS_VALUE_MISMATCH: { title: "Value changed", description: "The stored value differs from the one you corrected. Refresh and review.", needsRefresh: true },
  CORRECTED_VALUE_UNCHANGED: { title: "Nothing to correct", description: "A corrected value must differ from the current value.", needsRefresh: false },
  CORRECTED_VALUE_MISSING: { title: "Corrected value required", description: "Enter the corrected value.", needsRefresh: false },
  DUPLICATE_CORRECTION_TARGET: { title: "Duplicate correction", description: "Each field/line can be corrected only once per decision.", needsRefresh: false },
  CORRECTION_FIELD_NOT_ALLOWED: { title: "Field not correctable", description: "That field cannot be corrected.", needsRefresh: false },
  HEADER_FIELD_NOT_PRESENT: { title: "Field not present", description: "That header field is not present on this invoice.", needsRefresh: true },
  LINE_CORRECTION_LINE_NUMBER_REQUIRED: { title: "Line number required", description: "Select the invoice line to correct.", needsRefresh: false },
  HEADER_CORRECTION_LINE_NUMBER_PROHIBITED: { title: "Invalid target", description: "Header fields do not take a line number.", needsRefresh: false },
  RESOLVED_CASE_REQUIRED: { title: "Case not resolved", description: "Workflow resume needs an approved or corrected, resolved case.", needsRefresh: true },
  RESUME_DISPOSITION_INVALID: { title: "Not resumable", description: "Only approved or corrected decisions can resume the workflow.", needsRefresh: false },
  REVIEW_DECISION_REQUIRED: { title: "No decision recorded", description: "A resolved decision must exist before the workflow can resume.", needsRefresh: true },
  RESUME_REQUIRES_RESOLVED_DECISION: { title: "Not available in validation-only mode", description: "Workflow resume needs a persisted decision, so it is unavailable in validation-only mode.", needsRefresh: false },
  REVIEW_ACTION_NOT_SUPPORTED: { title: "Unsupported action", description: "That action is not supported. Payment, bank and ERP actions do not exist in this system.", needsRefresh: false },
  REVIEW_ACTION_NOT_IMPLEMENTED: { title: "Not implemented", description: "That action is not available yet.", needsRefresh: false },
  REQUEST_VALIDATION_FAILED: { title: "Invalid request", description: "The request was not valid. Check the highlighted fields.", needsRefresh: false },
  COMMAND_REQUEST_INVALID: { title: "Invalid request", description: "The request was not valid. Check the highlighted fields.", needsRefresh: false },
  REVIEW_COMMANDS_DISABLED: { title: "Review actions are disabled", description: "This deployment is read-only. No command was sent.", needsRefresh: false },
  COMMAND_MODE_MISMATCH: { title: "Command modes do not match", description: "The frontend and backend are configured for different command modes, so the request was not sent. Ask an administrator to align them.", needsRefresh: false },
  COMMAND_MODE_MISCONFIGURED: { title: "Command mode is misconfigured", description: "The frontend command-mode setting is invalid, so review actions are disabled.", needsRefresh: false },
  AUTHENTICATION_REQUIRED: { title: "Session ended", description: "Your session has ended. Sign in again; nothing was changed.", needsRefresh: false },
  ORGANIZATION_REQUIRED: { title: "Choose an organization", description: "Select an active organization, then try again. Nothing was changed.", needsRefresh: false },
  AUTHENTICATION_UNAVAILABLE: { title: "Sign-in could not be verified", description: "Your sign-in could not be verified right now. Nothing was changed; try again shortly.", needsRefresh: false },
  IDENTITY_NOT_MAPPED: { title: "Account not registered", description: "Your account is not registered for this organization. Nothing was changed.", needsRefresh: false },
  IDENTITY_MEMBERSHIP_INACTIVE: { title: "Access inactive", description: "Your access to this organization is inactive. Nothing was changed.", needsRefresh: false },
  TOKEN_EXPIRED: { title: "Session expired", description: "Your session has expired. Sign in again; nothing was changed.", needsRefresh: false },
  FRONTEND_AUTH_MISCONFIGURED: { title: "Authentication is misconfigured", description: "The server-side connection to the review API is not configured correctly.", needsRefresh: false },
  CSRF_ORIGIN_REJECTED: { title: "Request rejected", description: "The request did not come from this application. Reload the page and try again.", needsRefresh: true },
  CSRF_TOKEN_INVALID: { title: "Session expired", description: "The page's security token is missing or expired. Reload the page and try again.", needsRefresh: true },
  UNSUPPORTED_MEDIA_TYPE: { title: "Invalid request", description: "The request was not sent as JSON.", needsRefresh: false },
  REQUEST_TOO_LARGE: { title: "Request too large", description: "The request is too large. Shorten your notes or corrections.", needsRefresh: false },
  MALFORMED_JSON: { title: "Invalid request", description: "The request could not be read.", needsRefresh: false },
  MALFORMED_RESPONSE: { title: "Unexpected response", description: "The backend returned a response this application could not understand. The action may or may not have been recorded; refresh to check.", needsRefresh: true },
  AUTHENTICATION_FAILED: { title: "Not authenticated", description: "The review API rejected this application's credentials. Contact an administrator.", needsRefresh: false },
  TENANT_ACCESS_DENIED: { title: "Access denied", description: "This tenant is not served by this deployment.", needsRefresh: false },
  REVIEW_CASE_NOT_FOUND: { title: "Case not found", description: "This review case could not be found.", needsRefresh: false },
  COMMAND_TARGET_NOT_FOUND: { title: "Case not found", description: "This review case could not be found.", needsRefresh: false },
  DATABASE_UNAVAILABLE: { title: "Database unavailable", description: "The database is temporarily unavailable. Nothing was confirmed; refresh to check the current state before retrying.", needsRefresh: true },
  BACKEND_UNAVAILABLE: { title: "Backend unavailable", description: "The review API could not be reached. Nothing was confirmed; you can retry the same request safely.", needsRefresh: false },
  BACKEND_TIMEOUT: { title: "Backend timed out", description: "The review API did not respond in time. The action may not have been recorded; retrying the same request is safe.", needsRefresh: false },
  NETWORK_ERROR: { title: "Network error", description: "The request could not reach the server. You can retry the same request safely.", needsRefresh: false },
  INTERNAL_ERROR: { title: "Something went wrong", description: "An unexpected error occurred. Refresh to check whether the action was recorded.", needsRefresh: true },
};

const FALLBACK: ErrorPresentation = {
  title: "The action could not be completed",
  description: "The request was not accepted. Refresh to see the current state of this case.",
  needsRefresh: true,
};

function present(code: string): ErrorPresentation | null {
  const entry = M[code.split(":")[0] ?? code];
  if (!entry) return null;
  return { title: entry.title, description: entry.description, needsRefresh: entry.needsRefresh ?? false };
}

/** Field-level codes like `INVALID_FIELD:corrections.0.reason` have their own sentence. */
function presentFieldCode(code: string): ErrorPresentation | null {
  if (code.startsWith("INVALID_FIELD:") || code.startsWith("UNSUPPORTED_FIELD:")) {
    return { title: "Invalid request", description: "One or more fields were not valid.", needsRefresh: false };
  }
  return null;
}

/** All distinct presentations for a set of codes, most specific first (never raw codes). */
export function presentErrorCodes(codes: readonly string[]): ErrorPresentation[] {
  const seen = new Set<string>();
  const out: ErrorPresentation[] = [];
  for (const code of codes) {
    const presentation = present(code) ?? presentFieldCode(code);
    if (!presentation) continue;
    const key = `${presentation.title}|${presentation.description}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(presentation);
  }
  return out.length > 0 ? out : [FALLBACK];
}

/** True if any code means "reload current data before doing anything else". */
export function errorsNeedRefresh(codes: readonly string[]): boolean {
  return presentErrorCodes(codes).some((p) => p.needsRefresh);
}

/** Network-level failures where the *same* request may be retried verbatim. */
export function isSafeToRetry(httpStatus: number | null, codes: readonly string[]): boolean {
  if (httpStatus === null || httpStatus === 0) return true;
  return codes.some((code) => code === "BACKEND_UNAVAILABLE" || code === "BACKEND_TIMEOUT" || code === "NETWORK_ERROR");
}

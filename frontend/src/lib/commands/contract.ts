/**
 * Shared (server + browser) command contract for the M11C review actions.
 *
 * This module has no `server-only` import on purpose: the browser builds a
 * command draft from it, the Next.js route boundary re-validates the same
 * shape, and both parse the response with the same runtime validators.
 * Nothing here is authoritative policy -- FastAPI's
 * `validate_review_command` re-validates everything transactionally
 * (M11C task §2/§6) -- it is a fail-fast allow-list so an unsupported or
 * forged field never leaves the Next.js server (task §7).
 *
 * No unchecked assertions on upstream data (task §5): every response is
 * narrowed field by field from `unknown`.
 */

import type { InvoiceFieldNameValue, ReviewActionValue } from "@/types/api-payloads";

// ------------------------------------------------------------
// Actions (task §2 / §16)
// ------------------------------------------------------------

/** The only actions with a real transactional executor behind them. */
export const SUPPORTED_ACTIONS = ["CLAIM", "RELEASE", "ACCEPT", "CORRECT", "REJECT", "RESUME_WORKFLOW"] as const;
export type SupportedAction = (typeof SUPPORTED_ACTIONS)[number];

// Compile-time proof that every supported action is a real backend enum member.
const _supportedAreBackendActions: readonly ReviewActionValue[] = SUPPORTED_ACTIONS;
void _supportedAreBackendActions;

/** Authorised by backend role policy but with no executor: never exposed as a control (task §16). */
export const DEFERRED_ACTIONS = ["CONFIRM_SUPPLIER", "CONFIRM_PURCHASE_ORDER", "REQUEST_INFORMATION", "ESCALATE"] as const satisfies readonly ReviewActionValue[];

export type CommandDisposition = "APPROVED" | "CORRECTED" | "REJECTED";

/** The user-facing label for `ACCEPT` is "Approve" (task §12); the wire value stays `ACCEPT`. */
export const ACTION_LABELS: Record<SupportedAction, string> = {
  CLAIM: "Claim",
  RELEASE: "Release",
  ACCEPT: "Approve",
  CORRECT: "Correct",
  REJECT: "Reject",
  RESUME_WORKFLOW: "Request workflow resume",
};

export const ACTION_DISPOSITIONS: Record<SupportedAction, CommandDisposition | null | "STORED"> = {
  CLAIM: null,
  RELEASE: null,
  ACCEPT: "APPROVED",
  CORRECT: "CORRECTED",
  REJECT: "REJECTED",
  // Must equal the stored decision's disposition (APPROVED or CORRECTED).
  RESUME_WORKFLOW: "STORED",
};

export function isSupportedAction(value: unknown): value is SupportedAction {
  return typeof value === "string" && (SUPPORTED_ACTIONS as readonly string[]).includes(value);
}

// ------------------------------------------------------------
// Correctable fields (mirrors `default_interface_config`; the backend
// capability projection narrows this to fields actually present)
// ------------------------------------------------------------

export const HEADER_CORRECTION_FIELDS = [
  "SUPPLIER_NAME", "SUPPLIER_ADDRESS", "CUSTOMER_NAME", "CUSTOMER_ADDRESS", "INVOICE_NUMBER",
  "INVOICE_DATE", "DUE_DATE", "PURCHASE_ORDER_NUMBER", "CURRENCY", "SUBTOTAL", "TAX_AMOUNT",
  "DISCOUNT_AMOUNT", "SHIPPING_AMOUNT", "TOTAL_AMOUNT", "PAYMENT_TERMS",
] as const satisfies readonly InvoiceFieldNameValue[];
export const LINE_CORRECTION_FIELDS = [
  "LINE_DESCRIPTION", "LINE_QUANTITY", "LINE_UNIT_PRICE", "LINE_AMOUNT",
] as const satisfies readonly InvoiceFieldNameValue[];
export const ALL_CORRECTION_FIELDS: readonly InvoiceFieldNameValue[] = [...HEADER_CORRECTION_FIELDS, ...LINE_CORRECTION_FIELDS];

/** Narrows an untrusted string to a real `InvoiceFieldName` without an assertion. */
export function asInvoiceFieldName(value: unknown): InvoiceFieldNameValue | null {
  return typeof value === "string" ? (ALL_CORRECTION_FIELDS.find((field) => field === value) ?? null) : null;
}

/** Fields whose value is an exact decimal string; never coerced through `Number` (task §13). */
export const DECIMAL_FIELDS: ReadonlySet<string> = new Set([
  "SUBTOTAL", "TAX_AMOUNT", "DISCOUNT_AMOUNT", "SHIPPING_AMOUNT", "TOTAL_AMOUNT",
  "LINE_QUANTITY", "LINE_UNIT_PRICE", "LINE_AMOUNT",
]);

export const DECIMAL_PATTERN = /^-?\d+(\.\d+)?$/;

// ------------------------------------------------------------
// Limits shared with the route boundary
// ------------------------------------------------------------

export const IDEMPOTENCY_KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/;
export const UUID_PATTERN = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
export const REASON_CODE_PATTERN = /^[A-Z][A-Z0-9_]{1,63}$/;
export const EVIDENCE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}$/;
export const MAX_REASON_CODES = 10;
export const MAX_CORRECTIONS = 10;
export const MAX_EVIDENCE_PER_CORRECTION = 20;
export const MAX_NOTES_LENGTH = 4000;
export const MAX_CORRECTED_VALUE_LENGTH = 500;
export const MAX_CORRECTION_REASON_LENGTH = 1000;
export const MAX_REQUEST_BODY_BYTES = 64 * 1024;

// ------------------------------------------------------------
// Browser -> Next.js request (task §7: allow-listed fields only)
// ------------------------------------------------------------

export interface CorrectionDraft {
  field_name: string;
  line_number: number | null;
  previous_value: string | null;
  corrected_value: string;
  reason: string;
  evidence_reference_ids: string[];
}

/** Everything the browser may send. Tenant, actor, role, timestamps and auth headers are deliberately absent. */
export interface BrowserCommandRequest {
  command_id: string;
  idempotency_key: string;
  action: SupportedAction;
  disposition: CommandDisposition | null;
  observed_review_revision: number;
  observed_workflow_revision: number;
  reason_codes: string[];
  notes: string | null;
  corrections: CorrectionDraft[];
}

const REQUEST_KEYS: ReadonlySet<string> = new Set([
  "command_id", "idempotency_key", "action", "disposition", "observed_review_revision",
  "observed_workflow_revision", "reason_codes", "notes", "corrections",
]);
const CORRECTION_KEYS: ReadonlySet<string> = new Set([
  "field_name", "line_number", "previous_value", "corrected_value", "reason", "evidence_reference_ids",
]);

export type RequestParseResult =
  | { ok: true; value: BrowserCommandRequest }
  | { ok: false; errors: string[] };

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isPositiveInt(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 && value <= 2_147_483_647;
}

function stringArray(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  const out: string[] = [];
  for (const item of value) {
    if (typeof item !== "string") return null;
    out.push(item);
  }
  return out;
}

function parseCorrection(raw: unknown, index: number, errors: string[]): CorrectionDraft | null {
  const at = `corrections.${index}`;
  if (!isRecord(raw)) {
    errors.push(`INVALID_FIELD:${at}`);
    return null;
  }
  for (const key of Object.keys(raw)) {
    if (!CORRECTION_KEYS.has(key)) errors.push(`UNSUPPORTED_FIELD:${at}`);
  }
  const { field_name, line_number, previous_value, corrected_value, reason, evidence_reference_ids } = raw;
  let ok = true;

  if (asInvoiceFieldName(field_name) === null) {
    errors.push(`INVALID_FIELD:${at}.field_name`);
    ok = false;
  }
  if (line_number !== null && !isPositiveInt(line_number)) {
    errors.push(`INVALID_FIELD:${at}.line_number`);
    ok = false;
  }
  if (previous_value !== null && previous_value !== undefined && typeof previous_value !== "string") {
    errors.push(`INVALID_FIELD:${at}.previous_value`);
    ok = false;
  }
  if (typeof corrected_value !== "string" || corrected_value.length < 1 || corrected_value.length > MAX_CORRECTED_VALUE_LENGTH) {
    errors.push(`INVALID_FIELD:${at}.corrected_value`);
    ok = false;
  }
  if (typeof reason !== "string" || reason.length < 1 || reason.length > MAX_CORRECTION_REASON_LENGTH) {
    errors.push(`INVALID_FIELD:${at}.reason`);
    ok = false;
  }
  const evidence = stringArray(evidence_reference_ids);
  if (
    evidence === null ||
    evidence.length > MAX_EVIDENCE_PER_CORRECTION ||
    !evidence.every((id) => EVIDENCE_ID_PATTERN.test(id))
  ) {
    errors.push(`INVALID_FIELD:${at}.evidence_reference_ids`);
    ok = false;
  }

  if (!ok || typeof field_name !== "string" || typeof corrected_value !== "string" || typeof reason !== "string" || evidence === null) {
    return null;
  }
  return {
    field_name,
    line_number: typeof line_number === "number" ? line_number : null,
    previous_value: typeof previous_value === "string" ? previous_value : null,
    corrected_value,
    reason,
    evidence_reference_ids: evidence,
  };
}

/**
 * Validates an untrusted browser body against the allow-list. Returns stable,
 * input-free error codes (never echoes a submitted value).
 */
export function parseBrowserCommandRequest(raw: unknown): RequestParseResult {
  if (!isRecord(raw)) return { ok: false, errors: ["INVALID_REQUEST_BODY"] };

  const errors: string[] = [];
  for (const key of Object.keys(raw)) {
    if (!REQUEST_KEYS.has(key)) errors.push(`UNSUPPORTED_FIELD:${key.replace(/[^A-Za-z0-9_]/g, "?").slice(0, 40)}`);
  }

  const { command_id, idempotency_key, action, disposition, observed_review_revision, observed_workflow_revision } = raw;

  if (typeof command_id !== "string" || !UUID_PATTERN.test(command_id)) errors.push("INVALID_FIELD:command_id");
  if (typeof idempotency_key !== "string" || !IDEMPOTENCY_KEY_PATTERN.test(idempotency_key)) {
    errors.push("INVALID_FIELD:idempotency_key");
  }
  if (!isSupportedAction(action)) errors.push("REVIEW_ACTION_NOT_SUPPORTED");
  if (!isPositiveInt(observed_review_revision)) errors.push("INVALID_FIELD:observed_review_revision");
  if (!isPositiveInt(observed_workflow_revision)) errors.push("INVALID_FIELD:observed_workflow_revision");

  const reasonCodes = stringArray(raw.reason_codes ?? []);
  if (
    reasonCodes === null ||
    reasonCodes.length > MAX_REASON_CODES ||
    !reasonCodes.every((code) => REASON_CODE_PATTERN.test(code))
  ) {
    errors.push("INVALID_FIELD:reason_codes");
  }

  const notes = raw.notes ?? null;
  if (notes !== null && (typeof notes !== "string" || notes.length > MAX_NOTES_LENGTH)) errors.push("INVALID_FIELD:notes");

  const rawCorrections = raw.corrections ?? [];
  const corrections: CorrectionDraft[] = [];
  if (!Array.isArray(rawCorrections) || rawCorrections.length > MAX_CORRECTIONS) {
    errors.push("INVALID_FIELD:corrections");
  } else {
    rawCorrections.forEach((item, index) => {
      const parsed = parseCorrection(item, index, errors);
      if (parsed) corrections.push(parsed);
    });
  }

  if (isSupportedAction(action)) {
    const expected = ACTION_DISPOSITIONS[action];
    if (expected === "STORED") {
      if (disposition !== "APPROVED" && disposition !== "CORRECTED") errors.push("INVALID_FIELD:disposition");
    } else if ((disposition ?? null) !== expected) {
      errors.push("INVALID_FIELD:disposition");
    }
    if (action !== "CORRECT" && corrections.length > 0) errors.push("CORRECTIONS_NOT_ALLOWED");
  }

  if (errors.length > 0) return { ok: false, errors: [...new Set(errors)] };

  if (
    typeof command_id !== "string" || typeof idempotency_key !== "string" || !isSupportedAction(action) ||
    !isPositiveInt(observed_review_revision) || !isPositiveInt(observed_workflow_revision) ||
    reasonCodes === null || (notes !== null && typeof notes !== "string")
  ) {
    return { ok: false, errors: ["INVALID_REQUEST_BODY"] };
  }

  return {
    ok: true,
    value: {
      command_id,
      idempotency_key,
      action,
      disposition: disposition === "APPROVED" || disposition === "CORRECTED" || disposition === "REJECTED" ? disposition : null,
      observed_review_revision,
      observed_workflow_revision,
      reason_codes: reasonCodes,
      notes,
      corrections,
    },
  };
}

// ------------------------------------------------------------
// Responses (validated from `unknown`, never asserted)
// ------------------------------------------------------------

export interface ValidatedCommandData {
  kind: "VALIDATED";
  commandId: string;
  reviewCaseId: string;
  action: string;
  executionMode: "VALIDATION_ONLY";
  databaseMutation: false;
}

export interface ExecutedCommandData {
  kind: "ACCEPTED" | "IDEMPOTENT";
  commandId: string;
  reviewCaseId: string;
  resultingCaseStatus: string | null;
  resultingRevision: number | null;
  workflowResumed: boolean;
  decisionId: string | null;
  message: string;
  /** Present only on workflow-resume results. */
  resume: { restartStage: string | null; derivedVersion: string | null; resumePlanId: string | null } | null;
}

export type CommandSuccess = { requestId: string; data: ValidatedCommandData | ExecutedCommandData };

function str(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === "string" ? value : null;
}
function optStr(record: Record<string, unknown>, key: string): string | null | undefined {
  const value = record[key];
  if (value === undefined || value === null) return null;
  return typeof value === "string" ? value : undefined;
}

/** Parses a successful command envelope (`ApiEnvelope[ReviewCommandResponseData]`). Returns `null` if malformed. */
export function parseCommandSuccess(raw: unknown): CommandSuccess | null {
  if (!isRecord(raw)) return null;
  const requestId = str(raw, "request_id");
  const status = str(raw, "status");
  const data = raw.data;
  if (requestId === null || status === null || !isRecord(data)) return null;

  const commandId = str(data, "command_id");
  const reviewCaseId = str(data, "review_case_id");
  if (commandId === null || reviewCaseId === null) return null;

  if (status === "VALIDATED") {
    if (data.execution_mode !== "VALIDATION_ONLY" || data.database_mutation !== false) return null;
    const action = str(data, "action");
    if (action === null) return null;
    return {
      requestId,
      data: { kind: "VALIDATED", commandId, reviewCaseId, action, executionMode: "VALIDATION_ONLY", databaseMutation: false },
    };
  }

  if (status !== "ACCEPTED" && status !== "IDEMPOTENT") return null;

  const message = str(data, "message");
  const resultingCaseStatus = optStr(data, "resulting_case_status");
  const decisionId = optStr(data, "decision_id");
  const revision = data.resulting_revision;
  if (
    message === null || resultingCaseStatus === undefined || decisionId === undefined ||
    typeof data.workflow_resumed !== "boolean" ||
    !(revision === null || revision === undefined || typeof revision === "number")
  ) {
    return null;
  }

  let resume: ExecutedCommandData["resume"] = null;
  if ("restart_stage" in data || "derived_version" in data || "resume_plan_id" in data) {
    const restartStage = optStr(data, "restart_stage");
    const derivedVersion = optStr(data, "derived_version");
    const resumePlanId = optStr(data, "resume_plan_id");
    if (restartStage === undefined || derivedVersion === undefined || resumePlanId === undefined) return null;
    resume = { restartStage, derivedVersion, resumePlanId };
  }

  return {
    requestId,
    data: {
      kind: status,
      commandId,
      reviewCaseId,
      resultingCaseStatus,
      resultingRevision: typeof revision === "number" ? revision : null,
      workflowResumed: data.workflow_resumed,
      decisionId,
      message,
      resume,
    },
  };
}

const ERROR_CODE_PATTERN = /^[A-Z][A-Z0-9_]{1,63}(:[A-Za-z0-9_.]{1,80})?$/;

export interface CommandFailureBody {
  requestId: string | null;
  errors: string[];
}

/** Parses a controlled error envelope; only well-formed codes survive (never raw text). */
export function parseCommandFailure(raw: unknown): CommandFailureBody {
  if (!isRecord(raw)) return { requestId: null, errors: [] };
  const requestIdRaw = raw.request_id;
  const requestId = typeof requestIdRaw === "string" && UUID_PATTERN.test(requestIdRaw) ? requestIdRaw : null;
  const codes = Array.isArray(raw.errors) ? raw.errors : [];
  const errors = codes.filter((code): code is string => typeof code === "string" && ERROR_CODE_PATTERN.test(code));
  return { requestId, errors: [...new Set(errors)] };
}

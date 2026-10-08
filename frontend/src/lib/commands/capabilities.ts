import {
  DEFERRED_ACTIONS,
  SUPPORTED_ACTIONS,
  asInvoiceFieldName,
  isRecord,
  isSupportedAction,
  type SupportedAction,
} from "@/lib/commands/contract";
import type {
  AvailableActionPayload,
  CommandCapabilitiesPayload,
  CorrectableHeaderFieldPayload,
  CorrectableLineValuePayload,
  EvidenceOptionPayload,
} from "@/types/api-payloads";

/**
 * Runtime validation of `GET .../command-capabilities` (M11C task §5/§6).
 * The payload is advisory, but a malformed one must never silently enable a
 * control, so it is narrowed field by field and rejected wholesale (`null`)
 * on any deviation -- no `as` assertion on upstream data.
 */

const CASE_STATUSES = ["OPEN", "IN_REVIEW", "AWAITING_INFORMATION", "ESCALATED", "RESOLVED", "REJECTED"] as const;
const ASSIGNMENTS = ["UNASSIGNED", "ASSIGNED_TO_ACTOR", "ASSIGNED_TO_OTHER"] as const;
const DISPOSITIONS = ["APPROVED", "CORRECTED", "REJECTED", "HOLD", "NEEDS_INFORMATION"] as const;

function oneOf<T extends string>(value: unknown, allowed: readonly T[]): T | null {
  return typeof value === "string" ? (allowed.find((candidate) => candidate === value) ?? null) : null;
}

function strings(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  return value.every((item) => typeof item === "string") ? value.filter((item): item is string => typeof item === "string") : null;
}

function posInt(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 ? value : null;
}

function nullableString(value: unknown): string | null | undefined {
  if (value === null) return null;
  return typeof value === "string" ? value : undefined;
}

const EVIDENCE_TYPES = ["SOURCE_DOCUMENT", "EXTRACTED_FIELD", "FINANCIAL_CHECK"] as const;
const GENERIC_EVIDENCE_LABEL = "Evidence reference";

/**
 * Evidence choices (M11E.5). Each option is narrowed field by field; one that is malformed rejects the payload, and an option
 * whose id is not among `evidence_reference_ids` is dropped (a label can never widen what the backend accepts). An id with no
 * description (an older backend) is still offered, with a generic label -- never a bare UUID without context.
 */
function parseEvidenceOptions(raw: unknown, allowedIds: readonly string[]): EvidenceOptionPayload[] | null {
  const described = new Map<string, EvidenceOptionPayload>();
  if (raw !== undefined) {
    if (!Array.isArray(raw)) return null;
    for (const item of raw) {
      if (!isRecord(item)) return null;
      const evidenceType = oneOf(item.evidence_type, EVIDENCE_TYPES);
      const page = item.page_number === null || item.page_number === undefined ? null : posInt(item.page_number);
      const snippet = item.snippet === undefined ? null : nullableString(item.snippet);
      if (
        typeof item.reference_id !== "string" || item.reference_id === "" || evidenceType === null ||
        typeof item.label !== "string" || item.label === "" ||
        (item.page_number !== null && item.page_number !== undefined && page === null) || snippet === undefined
      ) {
        return null;
      }
      if (allowedIds.includes(item.reference_id) && !described.has(item.reference_id)) {
        described.set(item.reference_id, { reference_id: item.reference_id, evidence_type: evidenceType, label: item.label, page_number: page, snippet });
      }
    }
  }
  return allowedIds.map(
    (id) => described.get(id) ?? { reference_id: id, evidence_type: "EXTRACTED_FIELD" as const, label: GENERIC_EVIDENCE_LABEL, page_number: null, snippet: null },
  );
}

export function parseCapabilities(raw: unknown): CommandCapabilitiesPayload | null {
  if (!isRecord(raw)) return null;

  const commandMode = oneOf(raw.command_mode, ["COMMIT", "VALIDATION_ONLY"] as const);
  const caseStatus = oneOf(raw.case_status, CASE_STATUSES);
  const assignment = oneOf(raw.assignment, ASSIGNMENTS);
  const reviewRevision = posInt(raw.review_revision);
  const workflowRevision = posInt(raw.workflow_revision);
  const permitted = strings(raw.permitted_actions);
  const unsupported = strings(raw.unsupported_actions);
  if (
    commandMode === null || caseStatus === null || assignment === null || reviewRevision === null ||
    workflowRevision === null || permitted === null || unsupported === null ||
    typeof raw.actor_role !== "string" || raw.payment_execution !== "PROHIBITED"
  ) {
    return null;
  }

  // Only supported actions can ever be offered, whatever the payload claims.
  if (!Array.isArray(raw.available_actions)) return null;
  const available: AvailableActionPayload[] = [];
  for (const item of raw.available_actions) {
    if (!isRecord(item) || !isSupportedAction(item.action)) return null;
    const disposition = item.disposition === null ? null : oneOf(item.disposition, DISPOSITIONS);
    if (
      (item.disposition !== null && disposition === null) ||
      typeof item.requires_reason_codes !== "boolean" || typeof item.requires_notes !== "boolean" ||
      typeof item.requires_corrections !== "boolean"
    ) {
      return null;
    }
    available.push({
      action: item.action,
      disposition,
      requires_reason_codes: item.requires_reason_codes,
      requires_notes: item.requires_notes,
      requires_corrections: item.requires_corrections,
    });
  }

  const policy = raw.correction_policy;
  if (!isRecord(policy)) return null;
  const lineFields = strings(policy.line_fields);
  const evidenceIds = strings(policy.evidence_reference_ids);
  const lineNumbers = Array.isArray(policy.line_numbers) ? policy.line_numbers.map(posInt) : null;
  const evidenceOptions = evidenceIds === null ? null : parseEvidenceOptions(policy.evidence_options, evidenceIds);
  if (
    evidenceOptions === null ||
    lineFields === null || evidenceIds === null || lineNumbers === null || lineNumbers.includes(null) ||
    typeof policy.require_reason !== "boolean" || typeof policy.require_evidence !== "boolean" ||
    !Array.isArray(policy.header_fields) || !Array.isArray(policy.line_values)
  ) {
    return null;
  }

  const headerFields: CorrectableHeaderFieldPayload[] = [];
  for (const item of policy.header_fields) {
    const fieldName = isRecord(item) ? asInvoiceFieldName(item.field_name) : null;
    const current = isRecord(item) ? nullableString(item.current_value) : undefined;
    if (fieldName === null || current === undefined) return null;
    headerFields.push({ field_name: fieldName, current_value: current });
  }

  const lineValues: CorrectableLineValuePayload[] = [];
  for (const item of policy.line_values) {
    const fieldName = isRecord(item) ? asInvoiceFieldName(item.field_name) : null;
    const lineNumber = isRecord(item) ? posInt(item.line_number) : null;
    const current = isRecord(item) ? nullableString(item.current_value) : undefined;
    if (fieldName === null || lineNumber === null || current === undefined) return null;
    lineValues.push({ line_number: lineNumber, field_name: fieldName, current_value: current });
  }

  const resume = raw.resume;
  if (!isRecord(resume)) return null;
  const resumeDisposition = resume.disposition === null ? null : oneOf(resume.disposition, DISPOSITIONS);
  const decisionId = nullableString(resume.decision_id);
  const reason = nullableString(resume.ineligible_reason);
  if (
    typeof resume.eligible !== "boolean" || typeof resume.already_requested !== "boolean" ||
    (resume.disposition !== null && resumeDisposition === null) || decisionId === undefined || reason === undefined
  ) {
    return null;
  }

  return {
    command_mode: commandMode,
    actor_role: raw.actor_role,
    case_status: caseStatus,
    assignment,
    review_revision: reviewRevision,
    workflow_revision: workflowRevision,
    permitted_actions: permitted.filter(isSupportedAction),
    available_actions: available,
    unsupported_actions: DEFERRED_ACTIONS.filter((action) => unsupported.includes(action)),
    correction_policy: {
      header_fields: headerFields,
      line_fields: lineFields.flatMap((name) => {
        const field = asInvoiceFieldName(name);
        return field === null ? [] : [field];
      }),
      line_numbers: lineNumbers.filter((n): n is number => n !== null),
      line_values: lineValues,
      evidence_reference_ids: evidenceIds,
      evidence_options: evidenceOptions,
      require_reason: policy.require_reason,
      require_evidence: policy.require_evidence,
    },
    resume: {
      eligible: resume.eligible,
      already_requested: resume.already_requested,
      disposition: resumeDisposition,
      decision_id: decisionId,
      ineligible_reason: reason,
    },
    payment_execution: "PROHIBITED",
  };
}

export function availableSupportedActions(caps: CommandCapabilitiesPayload): SupportedAction[] {
  return SUPPORTED_ACTIONS.filter((action) => caps.available_actions.some((item) => item.action === action));
}

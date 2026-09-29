import {
  DECIMAL_FIELDS,
  DECIMAL_PATTERN,
  MAX_CORRECTED_VALUE_LENGTH,
  MAX_CORRECTION_REASON_LENGTH,
  MAX_CORRECTIONS,
  type CorrectionDraft,
} from "@/lib/commands/contract";
import type { CorrectionPolicyPayload } from "@/types/api-payloads";

/**
 * Correction-editor model and validation (M11C task §13). Pure and
 * unit-tested; mirrors -- never replaces -- the backend's
 * `validate_review_command`, so a reviewer gets inline feedback before the
 * request leaves the browser. Values stay exact strings: nothing here ever
 * parses a decimal into a JavaScript number, and a missing value is never
 * coerced to zero.
 */

export interface CorrectionRow {
  id: string;
  /** `H:<FIELD>` for a header field, `L:<line>:<FIELD>` for a line field; empty until chosen. */
  target: string;
  correctedValue: string;
  reason: string;
  evidenceIds: string[];
}

export interface CorrectionTarget {
  key: string;
  label: string;
  fieldName: string;
  lineNumber: number | null;
  currentValue: string | null;
}

export const MIN_REASON_LENGTH = 5;

export function emptyRow(id: string): CorrectionRow {
  return { id, target: "", correctedValue: "", reason: "", evidenceIds: [] };
}

/** Human label for an invoice field name, kept local so this module stays dependency-free. */
export function fieldLabel(fieldName: string): string {
  return fieldName
    .toLowerCase()
    .split("_")
    .map((word) => (word.length > 0 ? word[0]!.toUpperCase() + word.slice(1) : word))
    .join(" ");
}

/** The *only* targets a reviewer may pick -- exactly what the backend listed (no arbitrary field names / lines). */
export function correctionTargets(policy: CorrectionPolicyPayload): CorrectionTarget[] {
  const headers = policy.header_fields.map((field) => ({
    key: `H:${field.field_name}`,
    label: `${fieldLabel(field.field_name)} (header)`,
    fieldName: field.field_name,
    lineNumber: null,
    currentValue: field.current_value,
  }));
  const lines = [...policy.line_values]
    .sort((a, b) => a.line_number - b.line_number || a.field_name.localeCompare(b.field_name))
    .map((value) => ({
      key: `L:${value.line_number}:${value.field_name}`,
      label: `Line ${value.line_number} — ${fieldLabel(value.field_name.replace(/^LINE_/, ""))}`,
      fieldName: value.field_name,
      lineNumber: value.line_number,
      currentValue: value.current_value,
    }));
  return [...headers, ...lines];
}

export type RowErrors = Partial<Record<"target" | "correctedValue" | "reason" | "evidence", string>>;

export interface CorrectionValidation {
  rowErrors: Record<string, RowErrors>;
  formErrors: string[];
  valid: boolean;
}

export function validateCorrections(rows: readonly CorrectionRow[], policy: CorrectionPolicyPayload): CorrectionValidation {
  const targets = new Map(correctionTargets(policy).map((target) => [target.key, target]));
  const allowedEvidence = new Set(policy.evidence_reference_ids);
  const rowErrors: Record<string, RowErrors> = {};
  const formErrors: string[] = [];
  const seen = new Set<string>();

  if (rows.length === 0) formErrors.push("Add at least one correction.");
  if (rows.length > MAX_CORRECTIONS) formErrors.push(`A decision can include at most ${MAX_CORRECTIONS} corrections.`);

  for (const row of rows) {
    const errors: RowErrors = {};
    const target = targets.get(row.target);

    if (!target) {
      errors.target = "Choose a field or line to correct.";
    } else {
      if (seen.has(target.key)) errors.target = "This field is already being corrected.";
      seen.add(target.key);

      const value = row.correctedValue.trim();
      if (value.length === 0) {
        errors.correctedValue = "Enter the corrected value.";
      } else if (value.length > MAX_CORRECTED_VALUE_LENGTH) {
        errors.correctedValue = `The corrected value is limited to ${MAX_CORRECTED_VALUE_LENGTH} characters.`;
      } else if (DECIMAL_FIELDS.has(target.fieldName) && !DECIMAL_PATTERN.test(value)) {
        errors.correctedValue = "Enter an exact decimal number, for example 1250.00.";
      } else if (value === target.currentValue) {
        errors.correctedValue = "The corrected value must differ from the current value.";
      }
    }

    const reason = row.reason.trim();
    if (policy.require_reason && reason.length < MIN_REASON_LENGTH) {
      errors.reason = `Give a meaningful reason (at least ${MIN_REASON_LENGTH} characters).`;
    } else if (reason.length > MAX_CORRECTION_REASON_LENGTH) {
      errors.reason = `The reason is limited to ${MAX_CORRECTION_REASON_LENGTH} characters.`;
    }

    if (policy.require_evidence && row.evidenceIds.length === 0) {
      errors.evidence = "Select at least one supporting evidence reference.";
    } else if (!row.evidenceIds.every((id) => allowedEvidence.has(id))) {
      errors.evidence = "An evidence reference is not available on this case.";
    }

    if (Object.keys(errors).length > 0) rowErrors[row.id] = errors;
  }

  return { rowErrors, formErrors, valid: formErrors.length === 0 && Object.keys(rowErrors).length === 0 };
}

/** Builds wire corrections from *validated* rows. Returns `null` if any row does not resolve to a listed target. */
export function rowsToCorrections(rows: readonly CorrectionRow[], policy: CorrectionPolicyPayload): CorrectionDraft[] | null {
  const targets = new Map(correctionTargets(policy).map((target) => [target.key, target]));
  const out: CorrectionDraft[] = [];
  for (const row of rows) {
    const target = targets.get(row.target);
    if (!target) return null;
    out.push({
      field_name: target.fieldName,
      line_number: target.lineNumber,
      previous_value: target.currentValue,
      corrected_value: row.correctedValue.trim(),
      reason: row.reason.trim(),
      evidence_reference_ids: [...row.evidenceIds],
    });
  }
  return out;
}

/** True when the editor holds work that a Release would silently discard (task §11). */
export function hasUnsavedCorrections(rows: readonly CorrectionRow[]): boolean {
  return rows.some((row) => row.target !== "" || row.correctedValue !== "" || row.reason !== "" || row.evidenceIds.length > 0);
}

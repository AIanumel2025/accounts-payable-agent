"use client";

import { forwardRef, useId } from "react";
import {
  correctionTargets,
  emptyRow,
  type CorrectionRow,
  type CorrectionValidation,
} from "@/lib/commands/corrections";
import { MAX_CORRECTIONS } from "@/lib/commands/contract";
import type { CorrectionPolicyPayload } from "@/types/api-payloads";
import styles from "./ReviewActions.module.css";

interface Props {
  policy: CorrectionPolicyPayload;
  rows: CorrectionRow[];
  onRowsChange: (rows: CorrectionRow[]) => void;
  validation: CorrectionValidation | null;
  /** Evidence ids attached to each header field in the detail (suggestions only; never authoritative). */
  evidenceSuggestions: Record<string, string[]>;
  newRowId: () => string;
}

/**
 * Evidence-backed correction editor (M11C task §13). Only targets the
 * backend listed can be chosen; the original normalized value is displayed
 * read-only; evidence can only be picked from the case's own references.
 */
export const CorrectionEditor = forwardRef<HTMLFieldSetElement, Props>(function CorrectionEditor(
  { policy, rows, onRowsChange, validation, evidenceSuggestions, newRowId },
  ref,
) {
  const groupId = useId();
  const targets = correctionTargets(policy);
  const byKey = new Map(targets.map((target) => [target.key, target]));

  const update = (id: string, patch: Partial<CorrectionRow>) =>
    onRowsChange(rows.map((row) => (row.id === id ? { ...row, ...patch } : row)));

  return (
    <fieldset ref={ref} className={styles.fieldset} data-testid="correction-editor">
      <legend className={styles.legend}>Corrections</legend>
      <p className={styles.immutable}>
        The original normalized invoice record is immutable and is never edited. Submitting creates a derived, append-only correction decision instead.
      </p>

      {rows.map((row, index) => {
        const target = byKey.get(row.target);
        const errors = validation?.rowErrors[row.id] ?? {};
        const rowId = `${groupId}-${row.id}`;
        const suggested = new Set(target ? (evidenceSuggestions[target.fieldName] ?? []) : []);
        const evidenceOrder = [
          ...policy.evidence_reference_ids.filter((id) => suggested.has(id)),
          ...policy.evidence_reference_ids.filter((id) => !suggested.has(id)),
        ];

        return (
          <fieldset key={row.id} className={styles.fieldset} data-testid={`correction-row-${index}`}>
            <legend className={styles.legend}>Correction {index + 1}</legend>

            <label className={styles.field} htmlFor={`${rowId}-target`}>
              Field to correct
              <select
                id={`${rowId}-target`}
                className={styles.input}
                value={row.target}
                aria-invalid={errors.target ? true : undefined}
                aria-describedby={errors.target ? `${rowId}-target-error` : undefined}
                onChange={(event) => update(row.id, { target: event.target.value, correctedValue: "" })}
              >
                <option value="">Select a field…</option>
                {targets.map((option) => (
                  <option key={option.key} value={option.key}>{option.label}</option>
                ))}
              </select>
            </label>
            {errors.target ? <p id={`${rowId}-target-error`} className={styles.fieldError}>{errors.target}</p> : null}

            {target ? (
              <p className={styles.currentValue}>
                Original value (read-only):{" "}
                <span data-testid={`correction-current-${index}`}>{target.currentValue === null ? "not recorded" : target.currentValue}</span>
              </p>
            ) : null}

            <label className={styles.field} htmlFor={`${rowId}-value`}>
              Corrected value
              <input
                id={`${rowId}-value`}
                className={styles.input}
                type="text"
                inputMode={target && /QUANTITY|AMOUNT|PRICE|TOTAL|SUBTOTAL|TAX|DISCOUNT|SHIPPING/.test(target.fieldName) ? "decimal" : "text"}
                autoComplete="off"
                value={row.correctedValue}
                aria-invalid={errors.correctedValue ? true : undefined}
                aria-describedby={errors.correctedValue ? `${rowId}-value-error` : undefined}
                onChange={(event) => update(row.id, { correctedValue: event.target.value })}
              />
            </label>
            {errors.correctedValue ? <p id={`${rowId}-value-error`} className={styles.fieldError}>{errors.correctedValue}</p> : null}

            <label className={styles.field} htmlFor={`${rowId}-reason`}>
              Reason for this correction
              <textarea
                id={`${rowId}-reason`}
                className={styles.input}
                rows={2}
                value={row.reason}
                aria-invalid={errors.reason ? true : undefined}
                aria-describedby={errors.reason ? `${rowId}-reason-error` : undefined}
                onChange={(event) => update(row.id, { reason: event.target.value })}
              />
            </label>
            {errors.reason ? <p id={`${rowId}-reason-error`} className={styles.fieldError}>{errors.reason}</p> : null}

            <fieldset className={styles.fieldset} aria-describedby={errors.evidence ? `${rowId}-evidence-error` : undefined}>
              <legend className={styles.legend}>Supporting evidence</legend>
              {evidenceOrder.length === 0 ? (
                <p className={styles.note}>No evidence references are available on this case.</p>
              ) : (
                <div className={styles.evidenceList}>
                  {evidenceOrder.map((id) => (
                    <label key={id} className={styles.checkRow}>
                      <input
                        type="checkbox"
                        checked={row.evidenceIds.includes(id)}
                        onChange={(event) =>
                          update(row.id, {
                            evidenceIds: event.target.checked ? [...row.evidenceIds, id] : row.evidenceIds.filter((existing) => existing !== id),
                          })
                        }
                      />
                      <span>
                        {id}
                        {suggested.has(id) ? " (attached to this field)" : ""}
                      </span>
                    </label>
                  ))}
                </div>
              )}
              {errors.evidence ? <p id={`${rowId}-evidence-error`} className={styles.fieldError}>{errors.evidence}</p> : null}
            </fieldset>

            <div className={styles.actions}>
              <button type="button" className={styles.button} onClick={() => onRowsChange(rows.filter((existing) => existing.id !== row.id))}>
                Remove correction {index + 1}
              </button>
            </div>
          </fieldset>
        );
      })}

      <div className={styles.actions}>
        <button
          type="button"
          className={styles.button}
          disabled={rows.length >= MAX_CORRECTIONS}
          onClick={() => onRowsChange([...rows, emptyRow(newRowId())])}
        >
          Add another correction
        </button>
      </div>
    </fieldset>
  );
});

import { EmptyState } from "@/components/feedback/EmptyState";
import { fieldNameLabel, formatConfidence, valueTypeLabel } from "@/lib/formatting";
import { MISSING_VALUE_DISPLAY } from "@/lib/formatting/money";
import type { InterfaceFieldValuePayload } from "@/types/api-payloads";
import styles from "./DetailSections.module.css";

/**
 * Section B (M11B task §8B): normalized fields. A missing value is always
 * rendered as an explicit "not available" marker, never converted to zero
 * or an empty string that reads as "extracted but blank" (CLAUDE.md:
 * "Fixtures never enter production code" / task §8B). Exact decimal text
 * from the backend is preserved verbatim -- this table never parses
 * `raw_value`/`normalized_value` into a JS number.
 */
export function FieldsSection({ fields }: { fields: InterfaceFieldValuePayload[] }) {
  return (
    <section aria-labelledby="section-fields" className={styles.section}>
      <h2 id="section-fields" className={styles.sectionTitle}>
        Normalized fields
      </h2>
      {fields.length === 0 ? (
        <EmptyState title="No extracted fields" description="No normalized fields were recorded for this document." />
      ) : (
        <div className={styles.tableWrapper} tabIndex={0} role="region" aria-label="Normalized invoice fields, scrollable">
          <table className={styles.table}>
            <caption className="sr-only">Normalized invoice fields</caption>
            <thead>
              <tr>
                <th scope="col">Field</th>
                <th scope="col">Raw value</th>
                <th scope="col">Normalized value</th>
                <th scope="col">Type</th>
                <th scope="col">Confidence</th>
                <th scope="col">Review required</th>
                <th scope="col">Evidence</th>
              </tr>
            </thead>
            <tbody>
              {fields.map((fieldValue) => {
                const confidence = formatConfidence(fieldValue.confidence);
                return (
                  <tr key={fieldValue.field_name}>
                    <th scope="row">{fieldNameLabel(fieldValue.field_name)}</th>
                    <td className={fieldValue.raw_value === null ? styles.muted : undefined}>
                      {fieldValue.raw_value ?? MISSING_VALUE_DISPLAY}
                    </td>
                    <td className={fieldValue.normalized_value === null ? styles.muted : undefined}>
                      {fieldValue.normalized_value ?? MISSING_VALUE_DISPLAY}
                    </td>
                    <td>{valueTypeLabel(fieldValue.value_type)}</td>
                    <td
                      className={confidence.isMissing ? styles.muted : confidence.isLow ? styles.low : undefined}
                      aria-label={confidence.ariaLabel}
                    >
                      {confidence.display}
                    </td>
                    <td>{fieldValue.review_required ? "Yes" : "No"}</td>
                    <td>
                      {fieldValue.evidence_reference_ids.length > 0
                        ? `${fieldValue.evidence_reference_ids.length} reference${fieldValue.evidence_reference_ids.length === 1 ? "" : "s"}`
                        : MISSING_VALUE_DISPLAY}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

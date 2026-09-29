import { EmptyState } from "@/components/feedback/EmptyState";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { humanizeCode, validationStatusLabel } from "@/lib/formatting";
import { MISSING_VALUE_DISPLAY } from "@/lib/formatting/money";
import type { FinancialCheckPayload } from "@/types/api-payloads";
import styles from "./DetailSections.module.css";

/**
 * Section D (M11B task §8D): financial validation. `status` and
 * `check_type` are plain `str` on the backend (`FinancialCheckResponse`,
 * not a typed enum -- verified in `src/ap_agent/api/schemas.py`), so an
 * unrecognized future value still renders through `validationStatusLabel`'s
 * humanized fallback rather than crashing.
 */
export function FinancialChecksSection({ checks }: { checks: FinancialCheckPayload[] }) {
  return (
    <section aria-labelledby="section-financial" className={styles.section}>
      <h2 id="section-financial" className={styles.sectionTitle}>
        Financial validation
      </h2>
      {checks.length === 0 ? (
        <EmptyState
          title="No financial checks recorded"
          description="No financial validation checks were recorded for this document."
        />
      ) : (
        <div className={styles.tableWrapper} tabIndex={0} role="region" aria-label="Financial validation checks, scrollable">
          <table className={styles.table}>
            <caption className="sr-only">Financial validation checks</caption>
            <thead>
              <tr>
                <th scope="col">Check</th>
                <th scope="col">Status</th>
                <th scope="col">Message</th>
                <th scope="col">Expected</th>
                <th scope="col">Observed</th>
                <th scope="col">Evidence</th>
              </tr>
            </thead>
            <tbody>
              {checks.map((check) => (
                <tr key={check.check_id}>
                  <th scope="row">{humanizeCode(check.check_type)}</th>
                  <td>
                    <StatusBadge presentation={validationStatusLabel(check.status)} />
                  </td>
                  <td>{check.message}</td>
                  <td className={check.expected_value === null ? styles.muted : undefined}>
                    {check.expected_value ?? MISSING_VALUE_DISPLAY}
                  </td>
                  <td className={check.observed_value === null ? styles.muted : undefined}>
                    {check.observed_value ?? MISSING_VALUE_DISPLAY}
                  </td>
                  <td>
                    {check.evidence_reference_ids.length > 0
                      ? `${check.evidence_reference_ids.length} reference${check.evidence_reference_ids.length === 1 ? "" : "s"}`
                      : MISSING_VALUE_DISPLAY}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

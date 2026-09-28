import { formatInteger, formatReviewReason } from "@/lib/formatting";
import type { ReviewReasonCountPayload } from "@/types/api-payloads";
import { EmptyState } from "@/components/feedback/EmptyState";
import styles from "./ReviewReasonTable.module.css";

/**
 * Review-reason analytics (M11A task §9). A real table, not a decorative
 * chart (task §7: "decorative charts without operational value" are to be
 * avoided) -- counts are the operationally meaningful thing here.
 */
export function ReviewReasonTable({ reasons }: { reasons: ReviewReasonCountPayload[] }) {
  if (reasons.length === 0) {
    return (
      <EmptyState
        title="No review reasons recorded"
        description="No invoices are currently flagged for review, so there are no review reasons to show."
      />
    );
  }

  const sorted = [...reasons].sort((a, b) => b.count - a.count);

  return (
    <table className={styles.table}>
      <caption className={styles.caption}>Review-reason analytics</caption>
      <thead>
        <tr>
          <th scope="col">Reason</th>
          <th scope="col" className={styles.numericHeader}>
            Count
          </th>
        </tr>
      </thead>
      <tbody>
        {sorted.map((row) => {
          const formatted = formatInteger(row.count);
          return (
            <tr key={row.reason}>
              <th scope="row" className={styles.reasonCell}>
                {formatReviewReason(row.reason)}
              </th>
              <td className={styles.numericCell}>{formatted.display}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

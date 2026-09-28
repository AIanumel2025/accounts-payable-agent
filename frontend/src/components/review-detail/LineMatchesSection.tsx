import { EmptyState } from "@/components/feedback/EmptyState";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { formatReviewReason, matchStatusLabel } from "@/lib/formatting";
import { MISSING_VALUE_DISPLAY } from "@/lib/formatting/money";
import type { LineMatchPayload } from "@/types/api-payloads";
import styles from "./DetailSections.module.css";

/** Section F (M11B task §8F): line matches (invoice line ↔ purchase-order line). */
export function LineMatchesSection({ lineMatches }: { lineMatches: LineMatchPayload[] }) {
  return (
    <section aria-labelledby="section-line-matches" className={styles.section}>
      <h2 id="section-line-matches" className={styles.sectionTitle}>
        Line matches
      </h2>
      {lineMatches.length === 0 ? (
        <EmptyState
          title="No line matches recorded"
          description="No invoice-line to purchase-order-line matches were recorded for this document."
        />
      ) : (
        <div
          className={styles.tableWrapper}
          tabIndex={0}
          role="region"
          aria-label="Invoice line to purchase order line matches, scrollable"
        >
          <table className={styles.table}>
            <caption className="sr-only">Invoice line to purchase order line matches</caption>
            <thead>
              <tr>
                <th scope="col">Invoice line</th>
                <th scope="col">PO line</th>
                <th scope="col">Description</th>
                <th scope="col">Quantity</th>
                <th scope="col">Unit price</th>
                <th scope="col">Line total</th>
                <th scope="col">Review reasons</th>
              </tr>
            </thead>
            <tbody>
              {lineMatches.map((lineMatch) => (
                <tr key={lineMatch.line_match_id}>
                  <th scope="row">{lineMatch.invoice_line_number ?? MISSING_VALUE_DISPLAY}</th>
                  <td>{lineMatch.purchase_order_line_number ?? MISSING_VALUE_DISPLAY}</td>
                  <td>
                    <StatusBadge presentation={matchStatusLabel(lineMatch.description_status)} />
                  </td>
                  <td>
                    <StatusBadge presentation={matchStatusLabel(lineMatch.quantity_status)} />
                  </td>
                  <td>
                    <StatusBadge presentation={matchStatusLabel(lineMatch.unit_price_status)} />
                  </td>
                  <td>
                    <StatusBadge presentation={matchStatusLabel(lineMatch.line_total_status)} />
                  </td>
                  <td>
                    {lineMatch.review_reasons.length > 0 ? (
                      <ul className={styles.reasonList}>
                        {lineMatch.review_reasons.map((reason) => (
                          <li key={reason}>{formatReviewReason(reason)}</li>
                        ))}
                      </ul>
                    ) : (
                      <span className={styles.muted}>None</span>
                    )}
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

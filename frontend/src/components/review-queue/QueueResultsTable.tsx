import Link from "next/link";
import { StatusBadge } from "@/components/ui/StatusBadge";
import {
  formatAssignment,
  formatRelativeTimestamp,
  formatReviewReason,
  formatShortIdentifier,
  priorityLabel,
  reviewCaseStatusLabel,
  workflowStatusLabel,
} from "@/lib/formatting";
import type { ReviewQueueItemPayload } from "@/types/api-payloads";
import styles from "./QueueResultsTable.module.css";

/**
 * Review-queue results (M11B task §7). Renders as a real `<table>` on
 * wider viewports and an accessible card list on narrow ones (CSS-driven,
 * same data, task §7: "For smaller screens, use ... an accessible
 * card/list presentation containing the same essential information" --
 * never hiding review reasons, status or priority on mobile). Each row
 * has one explicit, meaningfully-labelled detail link -- never a
 * clickable `<tr>` (task §13).
 */
export function QueueResultsTable({ items, returnTo }: { items: ReviewQueueItemPayload[]; returnTo: string }) {
  return (
    <>
      <div className={styles.tableWrapper} data-testid="queue-table" tabIndex={0} role="region" aria-label="Review queue, scrollable">
      <table className={styles.table}>
        <caption className="sr-only">Review queue</caption>
        <thead>
          <tr>
            <th scope="col">Invoice</th>
            <th scope="col">Status</th>
            <th scope="col">Priority</th>
            <th scope="col">Review reasons</th>
            <th scope="col">Assigned</th>
            <th scope="col">Batch</th>
            <th scope="col">Updated</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <QueueTableRow key={item.review_case_id} item={item} returnTo={returnTo} />
          ))}
        </tbody>
      </table>
      </div>

      <ul className={styles.cardList} data-testid="queue-card-list">
        {items.map((item) => (
          <QueueCard key={item.review_case_id} item={item} returnTo={returnTo} />
        ))}
      </ul>
    </>
  );
}

function primaryLabel(item: ReviewQueueItemPayload): string {
  return item.invoice_number ?? item.source_name;
}

/** Appends the current queue URL (filters + page) as `?from=` so the detail page's breadcrumb can restore it (M11B task §2: "Preserve valid queue filters and page state when navigating back ... where practical"). */
function detailHref(reviewCaseId: string, returnTo: string): string {
  return `/review-cases/${reviewCaseId}?from=${encodeURIComponent(returnTo)}`;
}

function QueueTableRow({ item, returnTo }: { item: ReviewQueueItemPayload; returnTo: string }) {
  const label = primaryLabel(item);
  const assignment = formatAssignment(item.assigned_reviewer_id);
  const batch = formatShortIdentifier(item.batch_id);
  const updated = formatRelativeTimestamp(item.updated_at);

  return (
    <tr>
      <th scope="row" className={styles.identityCell}>
        <Link href={detailHref(item.review_case_id, returnTo)} aria-label={`Open review case for ${label}`}>
          {label}
        </Link>
        {item.supplier_name ? <span className={styles.supplier}>{item.supplier_name}</span> : null}
      </th>
      <td>
        <StatusBadge presentation={reviewCaseStatusLabel(item.case_status)} />
        <span className={styles.secondaryStatus}>{workflowStatusLabel(item.workflow_status).label}</span>
      </td>
      <td>
        <StatusBadge presentation={priorityLabel(item.priority)} />
      </td>
      <td>
        {item.review_reasons.length > 0 ? (
          <ul className={styles.reasonList}>
            {item.review_reasons.map((reason) => (
              <li key={reason}>{formatReviewReason(reason)}</li>
            ))}
          </ul>
        ) : (
          <span className={styles.muted}>None</span>
        )}
      </td>
      <td className={assignment.isUnassigned ? styles.muted : undefined}>{assignment.display}</td>
      <td>
        <span title={batch.full}>{batch.display}</span>
      </td>
      <td>
        <time dateTime={item.updated_at}>{updated}</time>
      </td>
    </tr>
  );
}

function QueueCard({ item, returnTo }: { item: ReviewQueueItemPayload; returnTo: string }) {
  const label = primaryLabel(item);
  const assignment = formatAssignment(item.assigned_reviewer_id);
  const updated = formatRelativeTimestamp(item.updated_at);

  return (
    <li className={styles.card}>
      <Link
        href={detailHref(item.review_case_id, returnTo)}
        className={styles.cardLink}
        aria-label={`Open review case for ${label}`}
      >
        {label}
      </Link>
      {item.supplier_name ? <p className={styles.supplier}>{item.supplier_name}</p> : null}
      <div className={styles.cardBadges}>
        <StatusBadge presentation={reviewCaseStatusLabel(item.case_status)} />
        <StatusBadge presentation={priorityLabel(item.priority)} />
      </div>
      {item.review_reasons.length > 0 ? (
        <ul className={styles.reasonList}>
          {item.review_reasons.map((reason) => (
            <li key={reason}>{formatReviewReason(reason)}</li>
          ))}
        </ul>
      ) : null}
      <dl className={styles.cardMeta}>
        <div>
          <dt>Assigned</dt>
          <dd className={assignment.isUnassigned ? styles.muted : undefined}>{assignment.display}</dd>
        </div>
        <div>
          <dt>Updated</dt>
          <dd>
            <time dateTime={item.updated_at}>{updated}</time>
          </dd>
        </div>
      </dl>
    </li>
  );
}

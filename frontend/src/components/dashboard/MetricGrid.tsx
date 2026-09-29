import { MetricCard } from "@/components/dashboard/MetricCard";
import type { DashboardPayload } from "@/types/api-payloads";
import styles from "./MetricGrid.module.css";

/**
 * The dashboard's required metric set (M11A task §9), in the order the
 * task brief lists them -- preserved verbatim from M11A (M11B task §9:
 * "Preserve the controlled-fixture baseline").
 *
 * Two cards link into the review queue (M11B task §11): "Review required"
 * (the queue's unfiltered contents -- every review-required invoice has a
 * corresponding review case) and "Open review cases" (`status=OPEN`, an
 * exact, backend-supported `ReviewCaseStatus` value -- verified in
 * `src/ap_agent/api/routes/review_cases.py`). "Unassigned review cases" is
 * deliberately left unlinked: `GET /api/v1/review-cases` has no
 * "unassigned" filter (only an exact `assigned_to` reviewer-id match), so
 * linking it would require fabricating a query parameter the backend
 * doesn't support (CLAUDE.md: "never invent parameter names or enum
 * values").
 */
export function MetricGrid({ dashboard }: { dashboard: DashboardPayload }) {
  return (
    <div className={styles.grid} role="list" aria-label="Invoice processing metrics">
      <div role="listitem">
        <MetricCard label="Total invoices" value={dashboard.total_invoices} tone="neutral" />
      </div>
      <div role="listitem">
        <MetricCard label="Processing invoices" value={dashboard.processing_invoices} tone="neutral" />
      </div>
      <div role="listitem">
        <MetricCard label="Completed automatically" value={dashboard.completed_invoices} tone="success" />
      </div>
      <div role="listitem">
        <MetricCard
          label="Review required"
          value={dashboard.review_required_invoices}
          tone="review-required"
          href="/review-queue"
        />
      </div>
      <div role="listitem">
        <MetricCard label="Failed invoices" value={dashboard.failed_invoices} tone="failed" />
      </div>
      <div role="listitem">
        <MetricCard
          label="Open review cases"
          value={dashboard.open_review_cases}
          tone="warning"
          href="/review-queue?status=OPEN"
        />
      </div>
      <div role="listitem">
        <MetricCard label="Unassigned review cases" value={dashboard.unassigned_review_cases} tone="warning" />
      </div>
    </div>
  );
}

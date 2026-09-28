import { MetricCard } from "@/components/dashboard/MetricCard";
import type { DashboardPayload } from "@/types/api-payloads";
import styles from "./MetricGrid.module.css";

/** The dashboard's required metric set (M11A task §9), in the order the task brief lists them. */
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
        <MetricCard label="Review required" value={dashboard.review_required_invoices} tone="review-required" />
      </div>
      <div role="listitem">
        <MetricCard label="Failed invoices" value={dashboard.failed_invoices} tone="failed" />
      </div>
      <div role="listitem">
        <MetricCard label="Open review cases" value={dashboard.open_review_cases} tone="warning" />
      </div>
      <div role="listitem">
        <MetricCard label="Unassigned review cases" value={dashboard.unassigned_review_cases} tone="warning" />
      </div>
    </div>
  );
}

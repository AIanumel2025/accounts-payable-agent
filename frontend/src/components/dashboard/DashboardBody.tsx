import { formatAbsoluteTimestamp } from "@/lib/formatting";
import { MetricGrid } from "@/components/dashboard/MetricGrid";
import { ReviewReasonTable } from "@/components/dashboard/ReviewReasonTable";
import type { DashboardPayload } from "@/types/api-payloads";
import styles from "./DashboardBody.module.css";

/** The dashboard's loaded state (M11A task §9/§10): metrics, review-reason analytics, last-refreshed time. */
export function DashboardBody({ dashboard }: { dashboard: DashboardPayload }) {
  const refreshedAt = formatAbsoluteTimestamp(dashboard.generated_at);

  return (
    <div className={styles.body} data-testid="dashboard-body">
      <section aria-labelledby="metrics-heading">
        <h2 id="metrics-heading" className={styles.sectionTitle}>
          Processing overview
        </h2>
        <MetricGrid dashboard={dashboard} />
      </section>

      <section aria-labelledby="reasons-heading">
        <h2 id="reasons-heading" className={styles.sectionTitle}>
          Review reasons
        </h2>
        <ReviewReasonTable reasons={dashboard.review_reason_counts} />
      </section>

      <p className={styles.refreshedAt}>
        Last refreshed{" "}
        <time dateTime={refreshedAt.isoUtc}>{refreshedAt.isValid ? refreshedAt.display : "unknown"}</time>
      </p>
    </div>
  );
}

import { EmptyState } from "@/components/feedback/EmptyState";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { dispositionLabel, formatAbsoluteTimestamp, formatReviewReason } from "@/lib/formatting";
import type { ReviewDecisionPayload } from "@/types/api-payloads";
import styles from "./DetailSections.module.css";

/**
 * Section H (M11B task §8H): previous review decisions. Strictly
 * historical and read-only -- no replay, edit, or delete action exists
 * here, matching the read-only guarantee (task §6): this milestone never
 * calls a command endpoint.
 */
export function ReviewDecisionsSection({ decisions }: { decisions: ReviewDecisionPayload[] }) {
  return (
    <section aria-labelledby="section-decisions" className={styles.section}>
      <h2 id="section-decisions" className={styles.sectionTitle}>
        Previous review decisions
      </h2>
      {decisions.length === 0 ? (
        <EmptyState
          title="No review decisions recorded"
          description="No human reviewer has recorded a decision on this case yet."
        />
      ) : (
        <ul className={styles.decisionList}>
          {decisions.map((decision) => {
            const decidedAt = formatAbsoluteTimestamp(decision.decided_at);
            return (
              <li key={decision.decision_id} className={styles.decisionCard}>
                <div className={styles.timelineHead}>
                  <StatusBadge presentation={dispositionLabel(decision.disposition)} />
                  <span className={styles.timelineMeta}>{decision.reviewer_id}</span>
                  <span className={styles.timelineMeta}>
                    <time dateTime={decidedAt.isoUtc}>{decidedAt.isValid ? decidedAt.display : "Unknown time"}</time>
                  </span>
                </div>
                {decision.reason_codes.length > 0 ? (
                  <ul className={styles.reasonList}>
                    {decision.reason_codes.map((reason) => (
                      <li key={reason}>{formatReviewReason(reason)}</li>
                    ))}
                  </ul>
                ) : null}
                <p className={styles.timelineMessage}>{decision.notes ?? "No notes recorded."}</p>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

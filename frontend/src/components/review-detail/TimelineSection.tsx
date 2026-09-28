import { EmptyState } from "@/components/feedback/EmptyState";
import { auditEventTypeLabel, formatAbsoluteTimestamp, stageLabel } from "@/lib/formatting";
import type { TimelineEventPayload } from "@/types/api-payloads";
import styles from "./DetailSections.module.css";

/**
 * Section G (M11B task §8G): timeline/audit history. Rendered in the exact
 * order the backend returns it -- never re-sorted client-side (CLAUDE.md:
 * "verbatim extraction", no silent reordering of backend-authored history).
 */
export function TimelineSection({ timeline }: { timeline: TimelineEventPayload[] }) {
  return (
    <section aria-labelledby="section-timeline" className={styles.section}>
      <h2 id="section-timeline" className={styles.sectionTitle}>
        Timeline
      </h2>
      {timeline.length === 0 ? (
        <EmptyState title="No timeline events recorded" description="No audit-history events were recorded for this document." />
      ) : (
        <ol className={styles.timelineList}>
          {timeline.map((event) => {
            const occurredAt = formatAbsoluteTimestamp(event.occurred_at);
            return (
              <li key={event.event_id} className={styles.timelineItem}>
                <div className={styles.timelineHead}>
                  <strong>{auditEventTypeLabel(event.event_type)}</strong>
                  {event.stage ? <span className={styles.timelineMeta}>{stageLabel(event.stage).label}</span> : null}
                  <span className={styles.timelineMeta}>{event.status}</span>
                </div>
                <p className={styles.timelineMessage}>{event.message}</p>
                <p className={styles.timelineMeta}>
                  <time dateTime={occurredAt.isoUtc}>{occurredAt.isValid ? occurredAt.display : "Unknown time"}</time>
                  {" · "}
                  {event.actor_id ?? "System"}
                </p>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

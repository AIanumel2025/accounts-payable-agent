import { EmptyState } from "@/components/feedback/EmptyState";
import { formatAbsoluteTimestamp, stageLabel } from "@/lib/formatting";
import { jobEventLabel } from "@/lib/operations/presentation";
import type { JobEventPayload } from "@/types/api-payloads";
import styles from "./Operations.module.css";

const GLYPH_BY_TYPE: Record<string, string> = {
  JOB_SUCCEEDED: "✓",
  JOB_REVIEW_REQUIRED: "!",
  JOB_FAILED: "✕",
  WORKFLOW_FAILED: "✕",
  STAGE_FAILED: "✕",
  STAGE_COMPLETED: "✓",
  REVIEW_ROUTED: "!",
};

/** Ordered event timeline (oldest first, exactly as the backend recorded it). */
export function JobTimeline({ events }: { events: readonly JobEventPayload[] }) {
  if (events.length === 0) {
    return <EmptyState title="No events yet" description="The worker has not reported anything for this job." />;
  }

  return (
    <ol className={styles.timeline} data-testid="job-timeline" aria-label="Job timeline">
      {events.map((event) => {
        const at = formatAbsoluteTimestamp(event.occurred_at);
        return (
          <li key={event.sequence_number} className={styles.timelineItem} data-event-type={event.event_type}>
            <span className={styles.timelineGlyph} aria-hidden="true">
              {GLYPH_BY_TYPE[event.event_type] ?? "•"}
            </span>
            <div>
              <div className={styles.timelineHead}>
                <strong>{jobEventLabel(event.event_type)}</strong>
                {event.stage ? <span className={styles.muted}>{stageLabel(event.stage).label}</span> : null}
                <time className={styles.muted} dateTime={at.isoUtc}>
                  {at.isValid ? at.display : "Unknown time"}
                </time>
              </div>
              <p className={styles.timelineMessage}>{event.message}</p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

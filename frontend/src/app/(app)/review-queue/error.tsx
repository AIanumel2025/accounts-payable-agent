"use client";

import { useEffect } from "react";
import styles from "./review-queue-page.module.css";
import feedbackStyles from "@/components/feedback/States.module.css";

/** Segment error boundary (M11B, mirrors M11A's dashboard/error.tsx). Never renders error.message/stack. */
export default function ReviewQueueError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error("Review queue segment error", error.digest ?? error.message);
  }, [error]);

  return (
    <div className={styles.content}>
      <div className={feedbackStyles.stateCard} role="alert" data-testid="error-state">
        <p className={feedbackStyles.stateGlyph} aria-hidden="true">
          ✕
        </p>
        <h2 className={feedbackStyles.stateTitle}>Something went wrong</h2>
        <p className={feedbackStyles.stateDescription}>
          An unexpected error occurred while loading the review queue. You can try again.
        </p>
        <button type="button" className={feedbackStyles.retryButton} onClick={reset}>
          Retry
        </button>
      </div>
    </div>
  );
}

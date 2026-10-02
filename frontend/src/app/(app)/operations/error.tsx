"use client";

import { useEffect } from "react";
import styles from "./operations-page.module.css";
import feedbackStyles from "@/components/feedback/States.module.css";

/** Segment error boundary; never renders `error.message`/`error.stack`. */
export default function OperationsError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error("Operations segment error", error.digest ?? error.message);
  }, [error]);

  return (
    <div className={styles.content}>
      <div className={feedbackStyles.stateCard} role="alert" data-testid="error-state">
        <p className={feedbackStyles.stateGlyph} aria-hidden="true">
          ✕
        </p>
        <h2 className={feedbackStyles.stateTitle}>Something went wrong</h2>
        <p className={feedbackStyles.stateDescription}>An unexpected error occurred while loading operations. You can try again.</p>
        <button type="button" className={feedbackStyles.retryButton} onClick={reset}>
          Retry
        </button>
      </div>
    </div>
  );
}

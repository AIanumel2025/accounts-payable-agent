"use client";

import { useEffect } from "react";
import styles from "./dashboard-page.module.css";
import feedbackStyles from "@/components/feedback/States.module.css";

/**
 * Segment error boundary (Next.js App Router convention; M11A task §10).
 * Catches anything `src/app/dashboard/page.tsx` doesn't handle as a typed
 * `AppResult` failure -- i.e. a genuinely unexpected exception. Never
 * renders `error.message`/`error.stack` (task §10: no stack trace,
 * internal exception message, or filesystem path reaches the user).
 */
export default function DashboardError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    // Server-side-only diagnostic logging; never rendered to the user.
    console.error("Dashboard segment error", error.digest ?? error.message);
  }, [error]);

  return (
    <div className={styles.content}>
      <div className={feedbackStyles.stateCard} role="alert" data-testid="error-state">
        <p className={feedbackStyles.stateGlyph} aria-hidden="true">
          ✕
        </p>
        <h2 className={feedbackStyles.stateTitle}>Something went wrong</h2>
        <p className={feedbackStyles.stateDescription}>
          An unexpected error occurred while loading the dashboard. You can try again.
        </p>
        <button type="button" className={feedbackStyles.retryButton} onClick={reset}>
          Retry
        </button>
      </div>
    </div>
  );
}

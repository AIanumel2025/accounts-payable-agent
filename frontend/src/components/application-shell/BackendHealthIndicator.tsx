"use client";

import { useState, useTransition } from "react";
import { backendHealthLabel } from "@/lib/formatting/status";
import { formatRelativeTimestamp } from "@/lib/formatting/timestamps";
import type { HealthPayload } from "@/types/api-payloads";
import styles from "./IndicatorGroup.module.css";

export interface BackendHealthState {
  reachable: boolean;
  data: HealthPayload | null;
  checkedAtIso: string;
}

/**
 * Backend connection indicator (M11A task §8/§9): shows live reachability
 * of the FastAPI backend, with a screen-reader-friendly status region and a
 * manual retry that goes through the server-side proxy route (never a
 * direct browser-to-FastAPI call).
 */
export function BackendHealthIndicator({ initial }: { initial: BackendHealthState }) {
  const [state, setState] = useState(initial);
  const [lastReachableIso, setLastReachableIso] = useState<string | null>(initial.reachable ? initial.checkedAtIso : null);
  const [isPending, startTransition] = useTransition();

  const presentation = backendHealthLabel(state.reachable ? "HEALTHY" : "UNAVAILABLE");
  const showStaleNote = !state.reachable && lastReachableIso !== null;

  function handleRetry() {
    startTransition(async () => {
      const checkedAtIso = new Date().toISOString();
      try {
        const response = await fetch("/api/backend/health", { cache: "no-store" });
        if (!response.ok) {
          setState({ reachable: false, data: null, checkedAtIso });
          return;
        }
        const envelope = (await response.json()) as { data: HealthPayload };
        setState({ reachable: true, data: envelope.data, checkedAtIso });
        setLastReachableIso(checkedAtIso);
      } catch {
        setState({ reachable: false, data: null, checkedAtIso });
      }
    });
  }

  return (
    <div className={styles.item} role="status" aria-live="polite">
      <span aria-hidden="true" className={styles.icon}>
        {presentation.glyph}
      </span>
      <span>
        <span className={styles.label}>Backend</span>
        <span className={styles.value}>
          {presentation.label}
          {showStaleNote ? (
            <span className={styles.staleNote} data-testid="backend-stale-note">
              {" "}
              · last connected {formatRelativeTimestamp(lastReachableIso)}
            </span>
          ) : null}
        </span>
      </span>
      <button
        type="button"
        className={styles.retryButton}
        onClick={handleRetry}
        disabled={isPending}
        data-testid="backend-health-retry"
      >
        {isPending ? "Checking…" : "Recheck"}
      </button>
    </div>
  );
}

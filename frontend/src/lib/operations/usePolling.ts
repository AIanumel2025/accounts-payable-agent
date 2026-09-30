"use client";

import { useEffect, useRef } from "react";

export const POLL_INTERVAL_MS = 2_000;
/** Bounded: ~5 minutes of automatic polling, after which the user refreshes manually. */
export const MAX_POLLS = 150;

/**
 * Calls `tick` every `intervalMs` while `active` and the page is visible.
 * Hidden pages pause (no background traffic) and refresh once on return.
 * Stops by itself after `MAX_POLLS` ticks.
 */
export function usePolling(tick: () => void | Promise<void>, active: boolean, intervalMs: number = POLL_INTERVAL_MS): void {
  const tickRef = useRef(tick);

  useEffect(() => {
    tickRef.current = tick;
  });

  useEffect(() => {
    if (!active) return undefined;

    let polls = 0;
    let timer: ReturnType<typeof setInterval> | null = null;

    const run = () => {
      polls += 1;
      if (polls > MAX_POLLS) {
        stop();
        return;
      }
      void tickRef.current();
    };

    const start = () => {
      if (timer === null) timer = setInterval(run, intervalMs);
    };

    const stop = () => {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    };

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        run();
        start();
      } else {
        stop();
      }
    };

    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [active, intervalMs]);
}

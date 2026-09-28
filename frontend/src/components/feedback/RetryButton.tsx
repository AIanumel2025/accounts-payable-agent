"use client";

import { useRouter } from "next/navigation";
import { useTransition } from "react";
import styles from "./States.module.css";

/** Re-runs the page's Server Components (a fresh backend request) without a full page reload (task §10). */
export function RetryButton({ label = "Retry" }: { label?: string }) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  return (
    <button
      type="button"
      className={styles.retryButton}
      disabled={isPending}
      onClick={() => startTransition(() => router.refresh())}
    >
      {isPending ? "Retrying…" : label}
    </button>
  );
}

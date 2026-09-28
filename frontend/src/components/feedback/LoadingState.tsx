import styles from "./States.module.css";

export function LoadingState({ label = "Loading dashboard data…" }: { label?: string }) {
  return (
    <div className={styles.stateCard} role="status" aria-live="polite" data-testid="loading-state">
      <span className={styles.spinner} aria-hidden="true" />
      <p className={styles.stateDescription}>{label}</p>
    </div>
  );
}

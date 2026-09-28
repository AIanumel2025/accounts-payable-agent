import styles from "./States.module.css";

export function EmptyState({ title, description }: { title: string; description: string }) {
  return (
    <div className={styles.stateCard} data-testid="empty-state">
      <p className={styles.stateGlyph} aria-hidden="true">
        ○
      </p>
      <h2 className={styles.stateTitle}>{title}</h2>
      <p className={styles.stateDescription}>{description}</p>
    </div>
  );
}

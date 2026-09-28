import type { AppErrorKind } from "@/lib/api/dashboard";
import { presentError } from "@/lib/api/error-messages";
import { RetryButton } from "@/components/feedback/RetryButton";
import styles from "./States.module.css";

export function ErrorState({ kind }: { kind: AppErrorKind }) {
  const presentation = presentError(kind);

  return (
    <div className={styles.stateCard} role="alert" data-testid="error-state">
      <p className={styles.stateGlyph} aria-hidden="true">
        ✕
      </p>
      <h2 className={styles.stateTitle}>{presentation.title}</h2>
      <p className={styles.stateDescription}>{presentation.description}</p>
      {presentation.retryable ? <RetryButton /> : null}
    </div>
  );
}

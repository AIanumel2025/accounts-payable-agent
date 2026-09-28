import type { StatusPresentation } from "@/lib/formatting/status";
import styles from "./StatusBadge.module.css";

/**
 * Renders a status as label + glyph + colour together (M11A task §7/§12:
 * "Status must never be communicated by colour alone.").
 */
export function StatusBadge({ presentation }: { presentation: StatusPresentation }) {
  return (
    <span className={`${styles.badge} ${styles[presentation.tone]}`}>
      <span aria-hidden="true" className={styles.glyph}>
        {presentation.glyph}
      </span>
      <span>{presentation.label}</span>
    </span>
  );
}

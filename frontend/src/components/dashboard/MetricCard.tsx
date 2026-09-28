import { formatInteger } from "@/lib/formatting/numbers";
import styles from "./MetricCard.module.css";

export interface MetricCardProps {
  label: string;
  value: number;
  tone?: "neutral" | "success" | "warning" | "review-required" | "failed";
  description?: string;
}

/** A single dashboard metric (M11A task §9). Compact, operational, no decorative chart. */
export function MetricCard({ label, value, tone = "neutral", description }: MetricCardProps) {
  const formatted = formatInteger(value);

  return (
    <div className={`${styles.card} ${styles[tone]}`}>
      <p className={styles.label}>{label}</p>
      <p className={styles.value} aria-label={`${label}: ${formatted.ariaLabel}`}>
        {formatted.display}
      </p>
      {description ? <p className={styles.description}>{description}</p> : null}
    </div>
  );
}

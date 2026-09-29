import Link from "next/link";
import { formatInteger } from "@/lib/formatting/numbers";
import styles from "./MetricCard.module.css";

export interface MetricCardProps {
  label: string;
  value: number;
  tone?: "neutral" | "success" | "warning" | "review-required" | "failed";
  description?: string;
  /** M11B task §11: when set, the whole card links to a real, exactly-supported review-queue filter -- never a fabricated one. */
  href?: string;
}

/** A single dashboard metric (M11A task §9). Compact, operational, no decorative chart. */
export function MetricCard({ label, value, tone = "neutral", description, href }: MetricCardProps) {
  const formatted = formatInteger(value);
  const className = `${styles.card} ${styles[tone]}`;
  // The accessible name lives in exactly one place: the linked variant puts
  // it on the enclosing <a> (so the whole card reads as one link) and omits
  // it from the inner <p>; the non-linked variant puts it on the <p> since
  // there is no enclosing link to carry it. Never both -- that produced two
  // elements matching the same accessible name (M11B fixed CI finding).
  const valueAriaLabel = href ? undefined : `${label}: ${formatted.ariaLabel}`;

  const body = (
    <>
      <p className={styles.label}>{label}</p>
      <p className={styles.value} aria-label={valueAriaLabel}>
        {formatted.display}
      </p>
      {description ? <p className={styles.description}>{description}</p> : null}
    </>
  );

  if (href) {
    return (
      <Link href={href} className={`${className} ${styles.linked}`} aria-label={`${label}: ${formatted.ariaLabel}`}>
        {body}
      </Link>
    );
  }

  return <div className={className}>{body}</div>;
}

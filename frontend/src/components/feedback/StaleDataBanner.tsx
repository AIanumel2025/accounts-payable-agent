import { formatAbsoluteTimestamp } from "@/lib/formatting";
import styles from "./States.module.css";

/** Stale-cached-data indicator (task §10): shown when we are displaying a previously successful fetch after a subsequent one failed. */
export function StaleDataBanner({ asOfIso }: { asOfIso: string }) {
  const timestamp = formatAbsoluteTimestamp(asOfIso);

  return (
    <div className={styles.staleBanner} role="status" data-testid="stale-data-banner">
      <span aria-hidden="true">⚠</span>
      <span>
        Showing data last refreshed <time dateTime={timestamp.isoUtc}>{timestamp.display}</time>. The most recent
        refresh attempt failed.
      </span>
    </div>
  );
}

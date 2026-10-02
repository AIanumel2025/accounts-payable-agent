import { LoadingState } from "@/components/feedback/LoadingState";
import styles from "./review-queue-page.module.css";

export default function ReviewQueueLoading() {
  return (
    <div className={styles.content}>
      <LoadingState label="Loading review queue…" />
    </div>
  );
}

import { LoadingState } from "@/components/feedback/LoadingState";
import styles from "./review-case-detail-page.module.css";

export default function ReviewCaseDetailLoading() {
  return (
    <div className={styles.content}>
      <LoadingState label="Loading review case…" />
    </div>
  );
}

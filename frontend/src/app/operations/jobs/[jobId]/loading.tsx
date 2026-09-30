import { LoadingState } from "@/components/feedback/LoadingState";
import styles from "./job-detail-page.module.css";

export default function JobDetailLoading() {
  return (
    <div className={styles.content}>
      <LoadingState />
    </div>
  );
}

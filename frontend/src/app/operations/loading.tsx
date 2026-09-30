import { LoadingState } from "@/components/feedback/LoadingState";
import styles from "./operations-page.module.css";

export default function OperationsLoading() {
  return (
    <div className={styles.content}>
      <LoadingState />
    </div>
  );
}

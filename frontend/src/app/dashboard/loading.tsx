import { LoadingState } from "@/components/feedback/LoadingState";
import styles from "./dashboard-page.module.css";

export default function DashboardLoading() {
  return (
    <div className={styles.content}>
      <LoadingState />
    </div>
  );
}

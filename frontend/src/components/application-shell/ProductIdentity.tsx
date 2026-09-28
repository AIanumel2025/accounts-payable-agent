import styles from "./ProductIdentity.module.css";

export function ProductIdentity() {
  return (
    <div className={styles.identity}>
      <span className={styles.mark} aria-hidden="true">
        AP
      </span>
      <span className={styles.name}>Accounts Payable Agent</span>
    </div>
  );
}

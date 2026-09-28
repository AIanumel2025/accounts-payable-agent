import styles from "./IndicatorGroup.module.css";

/**
 * Tenant-context indicator (M11A task §8). Deliberately does not render the
 * actual configured tenant UUID: task §16 requires that development tenant
 * identifiers never appear in browser output, so this shows only that a
 * single, fixed development tenant context is active -- never the value
 * itself. See docs/m11a_frontend_foundation_report.md, "Development-
 * authentication limitation".
 */
export function TenantContextIndicator() {
  return (
    <div className={styles.item} title="This deployment is scoped to one development tenant context">
      <span aria-hidden="true" className={styles.icon}>
        ▣
      </span>
      <span>
        <span className={styles.label}>Tenant</span>
        <span className={styles.value}>Development (single-tenant)</span>
      </span>
    </div>
  );
}

import styles from "./IndicatorGroup.module.css";

const ROLE_LABELS: Record<string, string> = {
  AP_OPERATOR: "AP Operator",
  AP_REVIEWER: "AP Reviewer",
  TENANT_ADMIN: "Tenant Admin",
  READ_ONLY_AUDITOR: "Read-Only Auditor",
};

/**
 * Actor-role indicator (M11A task §8). Shows the *role* the development
 * header-authentication adapter is currently acting as -- a role name is
 * category information the review interface is meant to surface ("who am
 * I acting as"), not a secret. It never shows the specific development
 * actor-id string (task §16 forbids that), which identifies a synthetic
 * local reviewer rather than a permission level.
 */
export function ActorRoleIndicator({ role }: { role: string }) {
  const label = ROLE_LABELS[role] ?? role;

  return (
    <div className={styles.item} title="Current development-authentication role">
      <span aria-hidden="true" className={styles.icon}>
        ◇
      </span>
      <span>
        <span className={styles.label}>Role</span>
        <span className={styles.value}>{label}</span>
      </span>
    </div>
  );
}

import styles from "./IndicatorGroup.module.css";

/**
 * Validation-only safety indicator (M11A task §6/§8): makes the read-only
 * guarantee visible in the UI itself, not just true in configuration.
 * `commandMode` comes from `/health`'s `command_mode`
 * (`ap_agent/api/routes/health.py`); when the backend is unreachable this
 * defaults to the safe assumption (validation-only), since M11A never
 * calls a write endpoint regardless of what the backend reports.
 */
export function ValidationOnlyIndicator({
  commandMode,
  frontendMode,
}: {
  commandMode: "COMMIT" | "VALIDATION_ONLY" | "UNKNOWN";
  /** M11C: the server-only frontend command mode. Omitted on pages that never offer actions (M11A/M11B behaviour). */
  frontendMode?: "disabled" | "validation_only" | "commit";
}) {
  if (frontendMode !== undefined) {
    const copy =
      frontendMode === "disabled"
        ? { icon: "🔒", value: "Read-only (actions disabled)", hint: "Review actions are disabled in this deployment" }
        : frontendMode === "validation_only"
          ? { icon: "🔒", value: "Validation-only (nothing executed)", hint: "Commands are validated but never executed" }
          : commandMode === "COMMIT"
            ? { icon: "⚠", value: "Commit mode (actions recorded)", hint: "Actions are executed and recorded in the database" }
            : { icon: "⚠", value: "Mode mismatch (actions blocked)", hint: "Frontend and backend command modes disagree" };
    return (
      <div className={styles.item} title={copy.hint}>
        <span aria-hidden="true" className={styles.icon}>{copy.icon}</span>
        <span>
          <span className={styles.label}>Mode</span>
          <span className={styles.value}>{copy.value}</span>
        </span>
      </div>
    );
  }

  const isValidationOnly = commandMode !== "COMMIT";

  return (
    <div className={styles.item} title="This interface never submits review commands in M11A">
      <span aria-hidden="true" className={styles.icon}>
        {isValidationOnly ? "🔒" : "⚠"}
      </span>
      <span>
        <span className={styles.label}>Mode</span>
        <span className={styles.value}>{isValidationOnly ? "Read-only / validation-only" : "Commands enabled"}</span>
      </span>
    </div>
  );
}

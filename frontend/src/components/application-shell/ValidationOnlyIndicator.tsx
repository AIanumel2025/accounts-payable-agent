import styles from "./IndicatorGroup.module.css";

/**
 * Validation-only safety indicator (M11A task §6/§8): makes the read-only
 * guarantee visible in the UI itself, not just true in configuration.
 * `commandMode` comes from `/health`'s `command_mode`
 * (`ap_agent/api/routes/health.py`); when the backend is unreachable this
 * defaults to the safe assumption (validation-only), since M11A never
 * calls a write endpoint regardless of what the backend reports.
 */
export function ValidationOnlyIndicator({ commandMode }: { commandMode: "COMMIT" | "VALIDATION_ONLY" | "UNKNOWN" }) {
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

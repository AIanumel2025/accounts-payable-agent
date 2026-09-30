import "server-only";

/**
 * Server-only operations-console mode (M11D Core).
 *
 * `AP_AGENT_FRONTEND_OPERATIONS_MODE` is deliberately not prefixed
 * `NEXT_PUBLIC_`. Default (unset/blank) is `disabled`; an unrecognised value
 * is treated as `disabled` and flagged invalid (fail closed). Independent of
 * the review-command mode: neither implies the other.
 */

export const OPERATIONS_MODE_VARIABLE = "AP_AGENT_FRONTEND_OPERATIONS_MODE";

export type FrontendOperationsMode = "disabled" | "enabled";

export interface OperationsModeConfig {
  mode: FrontendOperationsMode;
  /** False when the variable held an unrecognised value (mode is then `disabled`). */
  valid: boolean;
}

export type EnvSource = Record<string, string | undefined>;

export function loadOperationsMode(source: EnvSource = process.env): OperationsModeConfig {
  const raw = source[OPERATIONS_MODE_VARIABLE];
  if (raw === undefined || raw.trim() === "") return { mode: "disabled", valid: true };
  const value = raw.trim();
  if (value === "disabled" || value === "enabled") return { mode: value, valid: true };
  return { mode: "disabled", valid: false };
}

/** Fail-closed agreement: only `enabled` with a backend that reports `ENABLED`. */
export function operationsModesAgree(mode: FrontendOperationsMode, backendMode: string | null | undefined): boolean {
  return mode === "enabled" && backendMode === "ENABLED";
}

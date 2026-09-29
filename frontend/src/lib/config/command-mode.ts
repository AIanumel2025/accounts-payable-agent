import "server-only";
import type { HealthCommandMode } from "@/types/api-payloads";

/**
 * Server-only review-command mode (M11C task §3).
 *
 * `AP_AGENT_FRONTEND_REVIEW_COMMAND_MODE` is deliberately not prefixed
 * `NEXT_PUBLIC_`, so the browser can neither read nor change it. The default
 * (unset/blank) is `disabled`: an unconfigured deployment stays exactly as
 * read-only as M11B. An unrecognised value is treated as `disabled` and
 * flagged `valid: false` (fail closed), never guessed at.
 */

export const COMMAND_MODE_VARIABLE = "AP_AGENT_FRONTEND_REVIEW_COMMAND_MODE";

export type FrontendCommandMode = "disabled" | "validation_only" | "commit";

export interface CommandModeConfig {
  mode: FrontendCommandMode;
  /** False when the variable held an unrecognised value (mode is then `disabled`). */
  valid: boolean;
}

export type EnvSource = Record<string, string | undefined>;

export function loadCommandMode(source: EnvSource = process.env): CommandModeConfig {
  const raw = source[COMMAND_MODE_VARIABLE];
  if (raw === undefined || raw.trim() === "") return { mode: "disabled", valid: true };
  const value = raw.trim();
  if (value === "disabled" || value === "validation_only" || value === "commit") return { mode: value, valid: true };
  return { mode: "disabled", valid: false };
}

/** The backend `/health` mode a frontend mode may talk to; `null` means "never submit". */
export function expectedBackendMode(mode: FrontendCommandMode): HealthCommandMode | null {
  if (mode === "commit") return "COMMIT";
  if (mode === "validation_only") return "VALIDATION_ONLY";
  return null;
}

/** Fail-closed agreement check (task §3 "Mode mismatch"). */
export function modesAgree(mode: FrontendCommandMode, backendMode: string | null | undefined): boolean {
  const expected = expectedBackendMode(mode);
  return expected !== null && backendMode === expected;
}

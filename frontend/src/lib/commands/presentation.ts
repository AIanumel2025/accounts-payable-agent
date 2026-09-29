import { availableSupportedActions } from "@/lib/commands/capabilities";
import type { SupportedAction } from "@/lib/commands/contract";
import type { CommandCapabilitiesPayload } from "@/types/api-payloads";

/**
 * Decides what the action workspace shows (M11C task §9/§19). Pure and
 * side-effect free. UI visibility is a convenience, never a security
 * boundary (task §2): the backend re-authorises every command.
 */

export type FrontendModeName = "disabled" | "validation_only" | "commit";

export type WorkspaceState =
  | { kind: "disabled" }
  | { kind: "misconfigured" }
  | { kind: "capabilities_unavailable" }
  | { kind: "mode_mismatch" }
  | { kind: "no_permission" }
  | { kind: "ready"; mode: "validation_only" | "commit"; actions: SupportedAction[] };

export function backendModeFor(mode: FrontendModeName): "COMMIT" | "VALIDATION_ONLY" | null {
  return mode === "commit" ? "COMMIT" : mode === "validation_only" ? "VALIDATION_ONLY" : null;
}

export function deriveWorkspaceState(input: {
  frontendMode: FrontendModeName;
  modeValid: boolean;
  capabilities: CommandCapabilitiesPayload | null;
}): WorkspaceState {
  if (!input.modeValid) return { kind: "misconfigured" };
  if (input.frontendMode === "disabled") return { kind: "disabled" };
  if (input.capabilities === null) return { kind: "capabilities_unavailable" };
  if (input.capabilities.command_mode !== backendModeFor(input.frontendMode)) return { kind: "mode_mismatch" };
  if (input.capabilities.permitted_actions.length === 0) return { kind: "no_permission" };

  let actions = availableSupportedActions(input.capabilities);
  // Resume needs a persisted decision, which validation-only mode never has.
  if (input.frontendMode === "validation_only") actions = actions.filter((action) => action !== "RESUME_WORKFLOW");
  return { kind: "ready", mode: input.frontendMode, actions };
}

export const ROLE_LABELS: Record<string, string> = {
  AP_REVIEWER: "AP reviewer",
  AP_OPERATOR: "AP operator",
  TENANT_ADMIN: "Tenant administrator",
  READ_ONLY_AUDITOR: "Read-only auditor",
};

export function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? "Unknown role";
}

export const ASSIGNMENT_LABELS = {
  UNASSIGNED: "Unassigned",
  ASSIGNED_TO_ACTOR: "Claimed by you",
  ASSIGNED_TO_OTHER: "Claimed by another reviewer",
} as const;

export const RESUME_INELIGIBLE_COPY: Record<string, string> = {
  COMMAND_MODE_VALIDATION_ONLY: "Workflow resume is unavailable in validation-only mode.",
  ACTION_NOT_PERMITTED: "Your role cannot request a workflow resume.",
  REVIEW_DECISION_REQUIRED: "No decision has been recorded yet.",
  RESOLVED_CASE_REQUIRED: "The case must be resolved with an approved or corrected decision.",
  RESUME_DISPOSITION_INVALID: "Rejected decisions cannot resume the workflow.",
  DECISION_ACTOR_MISMATCH: "Only the reviewer who recorded the decision can request a resume.",
  RESUME_ALREADY_REQUESTED: "A workflow resume handoff was already created for this case.",
};

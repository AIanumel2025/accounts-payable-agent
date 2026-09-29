import { describe, expect, it } from "vitest";
import { expectedBackendMode, loadCommandMode, modesAgree } from "@/lib/config/command-mode";
import { backendModeFor, deriveWorkspaceState, roleLabel } from "@/lib/commands/presentation";
import { claimedCapabilities, makeCapabilities, resumeCapabilities } from "../support/command-fixtures";

describe("frontend command mode parsing (task §3)", () => {
  it("defaults to disabled when unset or blank", () => {
    expect(loadCommandMode({})).toEqual({ mode: "disabled", valid: true });
    expect(loadCommandMode({ AP_AGENT_FRONTEND_REVIEW_COMMAND_MODE: "   " })).toEqual({ mode: "disabled", valid: true });
  });

  it.each(["disabled", "validation_only", "commit"] as const)("accepts %s", (mode) => {
    expect(loadCommandMode({ AP_AGENT_FRONTEND_REVIEW_COMMAND_MODE: mode })).toEqual({ mode, valid: true });
  });

  it.each(["COMMIT", "Commit", "write", "true", "1", "validation-only", "commit;drop"])("fails closed on %s", (value) => {
    expect(loadCommandMode({ AP_AGENT_FRONTEND_REVIEW_COMMAND_MODE: value })).toEqual({ mode: "disabled", valid: false });
  });

  it("never reads a NEXT_PUBLIC_ variant", () => {
    expect(loadCommandMode({ NEXT_PUBLIC_AP_AGENT_FRONTEND_REVIEW_COMMAND_MODE: "commit" })).toEqual({ mode: "disabled", valid: true });
  });

  it("agrees only on the exact backend counterpart (mode mismatch fails closed)", () => {
    expect(modesAgree("commit", "COMMIT")).toBe(true);
    expect(modesAgree("validation_only", "VALIDATION_ONLY")).toBe(true);
    expect(modesAgree("validation_only", "COMMIT")).toBe(false);
    expect(modesAgree("commit", "VALIDATION_ONLY")).toBe(false);
    expect(modesAgree("disabled", "COMMIT")).toBe(false);
    expect(modesAgree("disabled", "VALIDATION_ONLY")).toBe(false);
    expect(modesAgree("commit", null)).toBe(false);
    expect(modesAgree("commit", undefined)).toBe(false);
    expect(expectedBackendMode("disabled")).toBeNull();
    expect(backendModeFor("commit")).toBe("COMMIT");
  });
});

describe("workspace state derivation (task §9/§19)", () => {
  const ready = (mode: "commit" | "validation_only", caps = claimedCapabilities()) =>
    deriveWorkspaceState({ frontendMode: mode, modeValid: true, capabilities: caps });

  it("is disabled/misconfigured/unavailable before anything else", () => {
    expect(deriveWorkspaceState({ frontendMode: "commit", modeValid: false, capabilities: claimedCapabilities() }).kind).toBe("misconfigured");
    expect(deriveWorkspaceState({ frontendMode: "disabled", modeValid: true, capabilities: claimedCapabilities() }).kind).toBe("disabled");
    expect(deriveWorkspaceState({ frontendMode: "commit", modeValid: true, capabilities: null }).kind).toBe("capabilities_unavailable");
  });

  it("detects mode mismatch in both directions", () => {
    expect(ready("validation_only", claimedCapabilities({ command_mode: "COMMIT" })).kind).toBe("mode_mismatch");
    expect(ready("commit", claimedCapabilities({ command_mode: "VALIDATION_ONLY" })).kind).toBe("mode_mismatch");
  });

  it("shows no permission for a role with no permitted actions", () => {
    expect(ready("commit", makeCapabilities({ permitted_actions: [], available_actions: [] })).kind).toBe("no_permission");
  });

  it("lists exactly the backend-available actions", () => {
    const state = ready("commit");
    expect(state).toEqual({ kind: "ready", mode: "commit", actions: ["RELEASE", "ACCEPT", "CORRECT", "REJECT"] });
  });

  it("removes workflow resume in validation-only mode", () => {
    const caps = resumeCapabilities("APPROVED", { command_mode: "VALIDATION_ONLY" });
    expect(ready("validation_only", caps)).toEqual({ kind: "ready", mode: "validation_only", actions: [] });
    expect(ready("commit", resumeCapabilities("APPROVED")).kind).toBe("ready");
  });

  it("maps role labels and never echoes unknown roles", () => {
    expect(roleLabel("AP_REVIEWER")).toBe("AP reviewer");
    expect(roleLabel("SOMETHING_ELSE")).toBe("Unknown role");
  });
});

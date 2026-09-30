import { describe, expect, it } from "vitest";
import { loadOperationsMode, operationsModesAgree } from "@/lib/config/operations-mode";

describe("operations mode (M11D Core)", () => {
  it("defaults to disabled when unset or blank", () => {
    expect(loadOperationsMode({})).toEqual({ mode: "disabled", valid: true });
    expect(loadOperationsMode({ AP_AGENT_FRONTEND_OPERATIONS_MODE: "  " })).toEqual({ mode: "disabled", valid: true });
  });

  it("accepts only disabled or enabled; anything else is disabled and flagged", () => {
    expect(loadOperationsMode({ AP_AGENT_FRONTEND_OPERATIONS_MODE: "enabled" })).toEqual({ mode: "enabled", valid: true });
    for (const value of ["true", "1", "ENABLED", "commit", "yes"]) {
      expect(loadOperationsMode({ AP_AGENT_FRONTEND_OPERATIONS_MODE: value })).toEqual({ mode: "disabled", valid: false });
    }
  });

  it("is independent of the review-command mode", () => {
    expect(loadOperationsMode({ AP_AGENT_FRONTEND_REVIEW_COMMAND_MODE: "commit" }).mode).toBe("disabled");
  });

  it("agrees only when both sides are enabled", () => {
    expect(operationsModesAgree("enabled", "ENABLED")).toBe(true);
    expect(operationsModesAgree("enabled", "DISABLED")).toBe(false);
    expect(operationsModesAgree("enabled", undefined)).toBe(false);
    expect(operationsModesAgree("enabled", null)).toBe(false);
    expect(operationsModesAgree("disabled", "ENABLED")).toBe(false);
  });
});

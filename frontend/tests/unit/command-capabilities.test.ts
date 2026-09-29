import { describe, expect, it } from "vitest";
import { availableSupportedActions, parseCapabilities } from "@/lib/commands/capabilities";
import { claimedCapabilities, makeCapabilities } from "../support/command-fixtures";

describe("capabilities parsing (task §6)", () => {
  it("round-trips a valid payload", () => {
    const parsed = parseCapabilities(claimedCapabilities());
    expect(parsed?.assignment).toBe("ASSIGNED_TO_ACTOR");
    expect(parsed && availableSupportedActions(parsed)).toEqual(["RELEASE", "ACCEPT", "CORRECT", "REJECT"]);
  });

  it("never lets a deferred or unknown action become a control", () => {
    const tampered = { ...makeCapabilities(), available_actions: [{ action: "ESCALATE", disposition: "HOLD", requires_reason_codes: true, requires_notes: true, requires_corrections: false }] };
    expect(parseCapabilities(tampered)).toBeNull();
    const payment = { ...makeCapabilities(), available_actions: [{ action: "EXECUTE_PAYMENT", disposition: null, requires_reason_codes: false, requires_notes: false, requires_corrections: false }] };
    expect(parseCapabilities(payment)).toBeNull();
  });

  it("drops deferred actions from the permitted list", () => {
    const parsed = parseCapabilities({ ...makeCapabilities(), permitted_actions: ["CLAIM", "ESCALATE", "CONFIRM_SUPPLIER"] });
    expect(parsed?.permitted_actions).toEqual(["CLAIM"]);
  });

  it.each([
    null, "x", [], {},
    { ...makeCapabilities(), command_mode: "WRITE" },
    { ...makeCapabilities(), case_status: "WEIRD" },
    { ...makeCapabilities(), assignment: "EVERYONE" },
    { ...makeCapabilities(), review_revision: 0 },
    { ...makeCapabilities(), workflow_revision: "1" },
    { ...makeCapabilities(), payment_execution: "ALLOWED" },
    { ...makeCapabilities(), correction_policy: null },
    { ...makeCapabilities(), resume: { eligible: "yes" } },
  ])("rejects malformed payload #%#", (payload) => {
    expect(parseCapabilities(payload)).toBeNull();
  });

  it("does not carry tenant or actor identity", () => {
    const parsed = parseCapabilities({ ...makeCapabilities(), tenant_id: "x", actor_id: "y" });
    expect(JSON.stringify(parsed)).not.toMatch(/tenant_id|actor_id/);
  });
});

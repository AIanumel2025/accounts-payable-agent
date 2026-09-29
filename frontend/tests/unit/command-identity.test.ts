import { describe, expect, it } from "vitest";
import { IDEMPOTENCY_KEY_PATTERN, UUID_PATTERN } from "@/lib/commands/contract";
import { OperationIdentityTracker, commandContentKey, createCommandIdentity, type CommandContent } from "@/lib/commands/identity";

const content = (patch: Partial<CommandContent> = {}): CommandContent => ({
  action: "CLAIM", disposition: null, observed_review_revision: 1, observed_workflow_revision: 1,
  reason_codes: [], notes: null, corrections: [], ...patch,
});

describe("command identity (task §8)", () => {
  it("generates a UUID command id and a backend-valid idempotency key", () => {
    const identity = createCommandIdentity();
    expect(identity.commandId).toMatch(UUID_PATTERN);
    expect(identity.idempotencyKey).toMatch(IDEMPOTENCY_KEY_PATTERN);
    expect(identity.idempotencyKey.length).toBeGreaterThanOrEqual(8);
    expect(identity.idempotencyKey.length).toBeLessThanOrEqual(128);
  });

  it("never repeats identity across independent operations", () => {
    const seen = new Set(Array.from({ length: 200 }, () => createCommandIdentity().idempotencyKey));
    expect(seen.size).toBe(200);
  });

  it("reuses identity for an identical retry (network retry / double click)", () => {
    const tracker = new OperationIdentityTracker();
    const first = tracker.identityFor("case-1", content());
    const retry = tracker.identityFor("case-1", content());
    expect(retry).toBe(first);
    expect(retry.commandId).toBe(first.commandId);
    expect(retry.idempotencyKey).toBe(first.idempotencyKey);
  });

  it("is insensitive to reason-code and evidence ordering", () => {
    const tracker = new OperationIdentityTracker();
    const a = tracker.identityFor("c", content({ reason_codes: ["A_1", "B_2"] }));
    const b = tracker.identityFor("c", content({ reason_codes: ["B_2", "A_1"] }));
    expect(b).toBe(a);
  });

  it.each<[string, Partial<CommandContent>]>([
    ["a different action", { action: "RELEASE" }],
    ["a refreshed review revision", { observed_review_revision: 2 }],
    ["a refreshed workflow revision", { observed_workflow_revision: 2 }],
    ["different reason codes", { reason_codes: ["X_1"] }],
    ["different notes", { notes: "changed" }],
  ])("mints fresh identity for %s", (_label, patch) => {
    const tracker = new OperationIdentityTracker();
    const first = tracker.identityFor("case-1", content());
    const next = tracker.identityFor("case-1", content(patch));
    expect(next.commandId).not.toBe(first.commandId);
    expect(next.idempotencyKey).not.toBe(first.idempotencyKey);
  });

  it("mints fresh identity for a different case and changed corrections", () => {
    const tracker = new OperationIdentityTracker();
    const first = tracker.identityFor("case-1", content());
    expect(tracker.identityFor("case-2", content()).commandId).not.toBe(first.commandId);

    const correction = { field_name: "TOTAL_AMOUNT", line_number: null, previous_value: "1", corrected_value: "2", reason: "r", evidence_reference_ids: ["e"] };
    const a = tracker.identityFor("case-2", content({ action: "CORRECT", corrections: [correction] }));
    const b = tracker.identityFor("case-2", content({ action: "CORRECT", corrections: [{ ...correction, corrected_value: "3" }] }));
    expect(b.commandId).not.toBe(a.commandId);
  });

  it("serialises content canonically", () => {
    expect(commandContentKey("c", content())).toBe(commandContentKey("c", content()));
    expect(commandContentKey("c", content())).not.toBe(commandContentKey("d", content()));
  });

  it("rejects a generator that would violate the backend pattern", () => {
    expect(() => createCommandIdentity(() => "not-a-uuid")).toThrow();
  });
});

import { describe, expect, it } from "vitest";
import {
  correctionTargets,
  emptyRow,
  hasUnsavedCorrections,
  rowsToCorrections,
  validateCorrections,
  type CorrectionRow,
} from "@/lib/commands/corrections";
import { makeCapabilities } from "../support/command-fixtures";

const policy = makeCapabilities().correction_policy;
const row = (patch: Partial<CorrectionRow> = {}): CorrectionRow => ({
  id: "r1", target: "H:PURCHASE_ORDER_NUMBER", correctedValue: "99A", reason: "Verified against the PO.", evidenceIds: ["ev-po-1"], ...patch,
});

describe("correction targets (task §13)", () => {
  it("offers only backend-listed header and line targets", () => {
    const keys = correctionTargets(policy).map((target) => target.key);
    expect(keys).toEqual(["H:PURCHASE_ORDER_NUMBER", "H:TOTAL_AMOUNT", "L:1:LINE_DESCRIPTION", "L:1:LINE_QUANTITY"]);
  });

  it("carries the immutable current value, including a missing value as null", () => {
    const withNull = { ...policy, header_fields: [{ field_name: "SUPPLIER_NAME" as const, current_value: null }] };
    expect(correctionTargets(withNull)[0]?.currentValue).toBeNull();
  });
});

describe("correction validation", () => {
  it("accepts a valid correction", () => {
    expect(validateCorrections([row()], policy).valid).toBe(true);
  });

  it("requires at least one correction", () => {
    const result = validateCorrections([], policy);
    expect(result.valid).toBe(false);
    expect(result.formErrors).toContain("Add at least one correction.");
  });

  it("rejects arbitrary field names and unknown lines", () => {
    expect(validateCorrections([row({ target: "H:ACCOUNT_NUMBER" })], policy).rowErrors.r1?.target).toBeDefined();
    expect(validateCorrections([row({ target: "L:99:LINE_QUANTITY", correctedValue: "6" })], policy).rowErrors.r1?.target).toBeDefined();
    expect(validateCorrections([row({ target: "" })], policy).valid).toBe(false);
  });

  it("requires a meaningful reason", () => {
    expect(validateCorrections([row({ reason: "" })], policy).rowErrors.r1?.reason).toBeDefined();
    expect(validateCorrections([row({ reason: "  ok " })], policy).rowErrors.r1?.reason).toBeDefined();
  });

  it("requires supporting evidence and only from the case's own references", () => {
    expect(validateCorrections([row({ evidenceIds: [] })], policy).rowErrors.r1?.evidence).toBeDefined();
    expect(validateCorrections([row({ evidenceIds: ["ev-not-on-this-case"] })], policy).rowErrors.r1?.evidence).toBeDefined();
  });

  it("requires exact decimal strings for numeric fields and never coerces", () => {
    const total = (value: string) => validateCorrections([row({ target: "H:TOTAL_AMOUNT", correctedValue: value })], policy).rowErrors.r1?.correctedValue;
    expect(total("100.100")).toBeUndefined();
    expect(total("1e3")).toBeDefined();
    expect(total("$100")).toBeDefined();
    expect(total("abc")).toBeDefined();
    expect(total("")).toBeDefined();
    expect(total("100.10")).toMatch(/differ/); // unchanged
  });

  it("does not treat a missing current value as zero", () => {
    const lineAmount = { ...policy, line_values: [{ line_number: 1, field_name: "LINE_AMOUNT" as const, current_value: null }] };
    const result = validateCorrections([row({ target: "L:1:LINE_AMOUNT", correctedValue: "0" })], lineAmount);
    expect(result.valid).toBe(true);
  });

  it("rejects duplicate targets and flags an oversized list", () => {
    const duplicate = validateCorrections([row({ id: "a" }), row({ id: "b" })], policy);
    expect(duplicate.rowErrors.b?.target).toMatch(/already/);
    const many = Array.from({ length: 11 }, (_, i) => row({ id: `r${i}` }));
    expect(validateCorrections(many, policy).formErrors.some((m) => m.includes("at most"))).toBe(true);
  });
});

describe("row conversion", () => {
  it("builds wire corrections with trimmed values, line numbers and previous values", () => {
    const wire = rowsToCorrections(
      [row({ correctedValue: " 99A " }), row({ id: "r2", target: "L:1:LINE_QUANTITY", correctedValue: "6" })],
      policy,
    );
    expect(wire).toEqual([
      { field_name: "PURCHASE_ORDER_NUMBER", line_number: null, previous_value: "99", corrected_value: "99A", reason: "Verified against the PO.", evidence_reference_ids: ["ev-po-1"] },
      { field_name: "LINE_QUANTITY", line_number: 1, previous_value: "5", corrected_value: "6", reason: "Verified against the PO.", evidence_reference_ids: ["ev-po-1"] },
    ]);
  });

  it("refuses to build a correction for an unlisted target", () => {
    expect(rowsToCorrections([row({ target: "H:NOT_LISTED" })], policy)).toBeNull();
  });

  it("detects unsaved work", () => {
    expect(hasUnsavedCorrections([])).toBe(false);
    expect(hasUnsavedCorrections([emptyRow("x")])).toBe(false);
    expect(hasUnsavedCorrections([{ ...emptyRow("x"), reason: "typed" }])).toBe(true);
  });
});

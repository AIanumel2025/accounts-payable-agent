import { describe, expect, it } from "vitest";
import { parseCapabilities } from "@/lib/commands/capabilities";
import { rowsToCorrections, validateCorrections, type CorrectionRow } from "@/lib/commands/corrections";
import { makeCapabilities } from "../support/command-fixtures";

// M11E.5: evidence choices are labelled, structured and closed -- a label can never widen what is accepted.
const SOURCE_ID = "0b3a3d52-6f0e-5b43-9d6e-2f6a7c1f4c11";
const SOURCE_LABEL = "Original source invoice — SHA-256 verified";

function rawWith(options: unknown, ids: string[] = [SOURCE_ID, "ev-po-1"]) {
  const base = makeCapabilities();
  return { ...base, correction_policy: { ...base.correction_policy, evidence_reference_ids: ids, evidence_options: options } };
}

const sourceOption = { reference_id: SOURCE_ID, evidence_type: "SOURCE_DOCUMENT", label: SOURCE_LABEL, page_number: null, snippet: null };
const fieldOption = { reference_id: "ev-po-1", evidence_type: "EXTRACTED_FIELD", label: "Extracted evidence — Purchase Order Number, page 1", page_number: 1, snippet: "PO 99" };

describe("evidence options in the capability payload", () => {
  it("keeps the labelled source-document option and the extracted-field option, in the backend's id order", () => {
    const policy = parseCapabilities(rawWith([sourceOption, fieldOption]))?.correction_policy;
    expect(policy?.evidence_options).toEqual([sourceOption, fieldOption]);
    expect(policy?.evidence_options[0]?.label).toBe(SOURCE_LABEL);
  });

  it("never offers a described option whose id the backend did not list (a label cannot widen acceptance)", () => {
    const rogue = { ...fieldOption, reference_id: "ev-rogue" };
    const policy = parseCapabilities(rawWith([sourceOption, rogue], [SOURCE_ID]))?.correction_policy;
    expect(policy?.evidence_options.map((option) => option.reference_id)).toEqual([SOURCE_ID]);
  });

  it("still offers an id without a description, with a generic label, when the backend sent no options", () => {
    const base = makeCapabilities();
    const { evidence_options: _omitted, ...withoutOptions } = base.correction_policy;
    const policy = parseCapabilities({ ...base, correction_policy: withoutOptions })?.correction_policy;
    expect(policy?.evidence_options.map((option) => [option.reference_id, option.label])).toEqual([
      ["ev-po-1", "Evidence reference"],
      ["ev-total-1", "Evidence reference"],
    ]);
  });

  it.each([
    ["a non-array", "nope"],
    ["an unknown evidence type", [{ ...sourceOption, evidence_type: "S3_OBJECT" }]],
    ["a missing label", [{ ...sourceOption, label: "" }]],
    ["a non-string reference id", [{ ...sourceOption, reference_id: 7 }]],
    ["a bad page number", [{ ...fieldOption, page_number: 0 }]],
    ["a non-string snippet", [{ ...fieldOption, snippet: 5 }]],
  ])("rejects the whole payload for %s (fail closed)", (_name, options) => {
    expect(parseCapabilities(rawWith(options))).toBeNull();
  });
});

describe("citing evidence in a correction", () => {
  const policy = parseCapabilities(rawWith([sourceOption, fieldOption]))!.correction_policy;
  const row = (evidenceIds: string[]): CorrectionRow => ({
    id: "r1", target: "H:PURCHASE_ORDER_NUMBER", correctedValue: "99A", reason: "Read from the paper invoice.", evidenceIds,
  });

  it("accepts the source-document option and sends ONLY its reference id", () => {
    expect(validateCorrections([row([SOURCE_ID])], policy).valid).toBe(true);
    const [correction] = rowsToCorrections([row([SOURCE_ID])], policy)!;
    expect(correction?.evidence_reference_ids).toEqual([SOURCE_ID]);
    const wire = JSON.stringify(correction);
    expect(wire).not.toContain("Original source invoice");
    expect(wire).not.toContain("SHA-256");
  });

  it("keeps extracted-field evidence valid", () => {
    expect(validateCorrections([row(["ev-po-1"])], policy).valid).toBe(true);
  });

  it("rejects free-form evidence identifiers and requires some evidence", () => {
    expect(validateCorrections([row(["any-id-i-like"])], policy).valid).toBe(false);
    expect(validateCorrections([row([SOURCE_ID, "forged"])], policy).valid).toBe(false);
    expect(validateCorrections([row([])], policy).valid).toBe(false);
  });
});

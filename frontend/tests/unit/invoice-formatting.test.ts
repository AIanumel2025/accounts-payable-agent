import { describe, expect, it } from "vitest";
import {
  auditEventTypeLabel,
  dispositionLabel,
  fieldNameLabel,
  formatAssignment,
  formatConfidence,
  formatShortIdentifier,
  matchStatusLabel,
  priorityLabel,
  validationStatusLabel,
  valueTypeLabel,
} from "@/lib/formatting";

describe("fieldNameLabel", () => {
  it("maps known InvoiceFieldName values", () => {
    expect(fieldNameLabel("SUPPLIER_NAME")).toBe("Supplier name");
    expect(fieldNameLabel("TOTAL_AMOUNT")).toBe("Total amount");
    expect(fieldNameLabel("PURCHASE_ORDER_NUMBER")).toBe("Purchase order number");
  });

  it("humanizes an unrecognized field name instead of throwing", () => {
    expect(fieldNameLabel("SOME_NEW_FIELD")).toBe("Some New Field");
  });
});

describe("valueTypeLabel", () => {
  it("maps every NormalizedValueType value", () => {
    expect(valueTypeLabel("TEXT")).toBe("Text");
    expect(valueTypeLabel("DATE")).toBe("Date");
    expect(valueTypeLabel("DECIMAL")).toBe("Decimal");
    expect(valueTypeLabel("CURRENCY_CODE")).toBe("Currency code");
  });
});

describe("formatConfidence", () => {
  it("distinguishes missing confidence from a low-but-present value", () => {
    const missing = formatConfidence(null);
    expect(missing.isMissing).toBe(true);
    expect(missing.isLow).toBe(false);

    const low = formatConfidence(0.4);
    expect(low.isMissing).toBe(false);
    expect(low.isLow).toBe(true);
    expect(low.display).toBe("40%");

    const high = formatConfidence(0.97);
    expect(high.isLow).toBe(false);
    expect(high.display).toBe("97%");
  });

  it("never renders a missing confidence as 0%", () => {
    expect(formatConfidence(undefined).display).not.toBe("0%");
    expect(formatConfidence(null).display).not.toBe("0%");
  });
});

describe("formatAssignment", () => {
  it("renders null/empty as explicitly Unassigned", () => {
    expect(formatAssignment(null)).toEqual({ display: "Unassigned", isUnassigned: true });
    expect(formatAssignment(undefined)).toEqual({ display: "Unassigned", isUnassigned: true });
    expect(formatAssignment("")).toEqual({ display: "Unassigned", isUnassigned: true });
  });

  it("renders an assigned reviewer id as-is", () => {
    expect(formatAssignment("reviewer-1")).toEqual({ display: "reviewer-1", isUnassigned: false });
  });
});

describe("formatShortIdentifier", () => {
  it("shortens a long identifier but preserves the full value", () => {
    const uuid = "11111111-2222-3333-4444-555555555555";
    const result = formatShortIdentifier(uuid);
    expect(result.display).toBe("11111111…");
    expect(result.full).toBe(uuid);
  });

  it("does not shorten a value already at or under the visible length", () => {
    const result = formatShortIdentifier("short", 8);
    expect(result.display).toBe("short");
    expect(result.full).toBe("short");
  });
});

describe("validationStatusLabel", () => {
  it("maps every observed financial-check status", () => {
    for (const code of ["PASSED", "FAILED", "REVIEW_REQUIRED", "SKIPPED", "NOT_APPLICABLE"]) {
      const presentation = validationStatusLabel(code);
      expect(presentation.label.length).toBeGreaterThan(0);
      expect(presentation.glyph.length).toBeGreaterThan(0);
    }
    expect(validationStatusLabel("PASSED").tone).toBe("success");
    expect(validationStatusLabel("FAILED").tone).toBe("failed");
  });
});

describe("matchStatusLabel", () => {
  it("maps every observed line-match/reference-match status", () => {
    expect(matchStatusLabel("MATCHED").tone).toBe("success");
    expect(matchStatusLabel("NOT_REFERENCED").tone).toBe("neutral");
    expect(matchStatusLabel("REVIEW_REQUIRED").tone).toBe("review-required");
  });

  it("falls back safely for an unrecognized status", () => {
    const presentation = matchStatusLabel("SOMETHING_NEW");
    expect(presentation.label).toBe("Something New");
  });
});

describe("priorityLabel", () => {
  it("maps every ReviewPriority value", () => {
    expect(priorityLabel("CRITICAL").label).toBe("Critical");
    expect(priorityLabel("HIGH").label).toBe("High");
    expect(priorityLabel("NORMAL").label).toBe("Normal");
    expect(priorityLabel("LOW").label).toBe("Low");
  });
});

describe("dispositionLabel", () => {
  it("maps every HumanReviewDisposition value", () => {
    expect(dispositionLabel("APPROVED").label).toBe("Approved");
    expect(dispositionLabel("REJECTED").label).toBe("Rejected");
    expect(dispositionLabel("HOLD").label).toBe("On hold");
    expect(dispositionLabel("NEEDS_INFORMATION").label).toBe("Needs information");
    expect(dispositionLabel("CORRECTED").label).toBe("Corrected");
  });
});

describe("auditEventTypeLabel", () => {
  it("maps every real MemoryEventType value (src/ap_agent/models/memory.py)", () => {
    expect(auditEventTypeLabel("MEMORY_CREATED")).toBe("Memory record created");
    expect(auditEventTypeLabel("WORKFLOW_TRANSITIONED")).toBe("Workflow transitioned");
    expect(auditEventTypeLabel("PHASE_RESULT_LINKED")).toBe("Phase result linked");
    expect(auditEventTypeLabel("REVIEW_DECISION_RECORDED")).toBe("Review decision recorded");
    expect(auditEventTypeLabel("ARTIFACT_VERIFIED")).toBe("Artifact verified");
    expect(auditEventTypeLabel("MEMORY_CONFLICT_REJECTED")).toBe("Memory conflict rejected");
  });

  it("humanizes an unrecognized event type instead of assuming a closed set", () => {
    expect(auditEventTypeLabel("SOME_FUTURE_EVENT")).toBe("Some Future Event");
  });
});

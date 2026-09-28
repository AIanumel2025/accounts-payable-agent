import { describe, expect, it } from "vitest";
import {
  formatAbsoluteTimestamp,
  formatInteger,
  formatMoney,
  formatPercentage,
  formatRelativeTimestamp,
  formatReviewReason,
  reviewCaseStatusLabel,
  workflowStatusLabel,
} from "@/lib/formatting";

describe("formatMoney", () => {
  it("preserves the exact decimal text received from the backend, unmodified", () => {
    expect(formatMoney("1234.500", "GBP").display).toBe("GBP 1234.500");
    expect(formatMoney("0.10", "GBP").display).toBe("GBP 0.10");
    expect(formatMoney("999999999999.999999", "USD").display).toBe("USD 999999999999.999999");
  });

  it("never converts a missing amount to zero", () => {
    const result = formatMoney(null, "GBP");
    expect(result.display).not.toBe("0");
    expect(result.display).not.toBe("0.00");
    expect(result.isMissing).toBe(true);
  });

  it("renders without a currency prefix when currency is missing", () => {
    expect(formatMoney("100.00", null).display).toBe("100.00");
  });
});

describe("formatInteger", () => {
  it("formats a plain count", () => {
    expect(formatInteger(4).display).toBe("4");
    expect(formatInteger(1000).display).toBe("1,000");
  });

  it("marks null/undefined as missing, not zero", () => {
    expect(formatInteger(null).isMissing).toBe(true);
    expect(formatInteger(undefined).isMissing).toBe(true);
    expect(formatInteger(0).isMissing).toBe(false);
    expect(formatInteger(0).display).toBe("0");
  });
});

describe("formatPercentage", () => {
  it("converts a proportion to a percentage by default", () => {
    expect(formatPercentage(0.5).display).toBe("50%");
    expect(formatPercentage(0.333, { fractionDigits: 1 }).display).toBe("33.3%");
  });

  it("accepts already-scaled percentage points", () => {
    expect(formatPercentage(75, { isPercentagePoints: true }).display).toBe("75%");
  });
});

describe("timestamps", () => {
  it("formats an absolute timestamp in Europe/London and preserves the original UTC ISO string", () => {
    const result = formatAbsoluteTimestamp("2026-06-15T12:00:00Z");
    expect(result.isValid).toBe(true);
    expect(result.isoUtc).toBe("2026-06-15T12:00:00Z");
    // BST (UTC+1) in June: 12:00 UTC displays as 13:00 local.
    expect(result.display).toContain("13:00");
  });

  it("reports invalid timestamps explicitly rather than throwing", () => {
    expect(formatAbsoluteTimestamp("not-a-date").isValid).toBe(false);
    expect(formatAbsoluteTimestamp(null).isValid).toBe(false);
  });

  it("formats relative time against an injected 'now'", () => {
    const now = new Date("2026-06-15T12:05:00Z");
    expect(formatRelativeTimestamp("2026-06-15T12:00:00Z", now)).toBe("5 minutes ago");
    expect(formatRelativeTimestamp("2026-06-15T12:04:58Z", now)).toBe("just now");
  });
});

describe("status labels", () => {
  it("never encodes status via tone alone -- every mapping has a label and a glyph", () => {
    for (const code of ["SUCCEEDED", "REVIEW_REQUIRED", "FAILED", "IN_PROGRESS"]) {
      const presentation = workflowStatusLabel(code);
      expect(presentation.label.length).toBeGreaterThan(0);
      expect(presentation.glyph.length).toBeGreaterThan(0);
    }
  });

  it("falls back to a humanized label for an unrecognized code instead of throwing", () => {
    const presentation = reviewCaseStatusLabel("SOME_NEW_STATUS");
    expect(presentation.label).toBe("Some New Status");
    expect(presentation.tone).toBe("neutral");
  });
});

describe("formatReviewReason", () => {
  it("maps the documented controlled-fixture reason codes", () => {
    expect(formatReviewReason("INHERITED_FINANCIAL_VALIDATION_REVIEW")).toBe(
      "Inherited financial validation review",
    );
    expect(formatReviewReason("SUPPLIER_NAME_MISSING")).toBe("Supplier name missing");
    expect(formatReviewReason("INVOICE_LINE_TOTAL_MISSING")).toBe("Invoice line total missing");
  });

  it("humanizes an unrecognized reason code instead of assuming a closed set", () => {
    expect(formatReviewReason("SOME_FUTURE_REASON_CODE")).toBe("Some Future Reason Code");
  });
});

/**
 * Review-reason label mapping (M11A task §11).
 *
 * The known review-reason codes below are the ones this milestone has
 * observed in the M10 API response shape and the controlled-fixture
 * acceptance baseline (task §9) -- they are a *display* convenience, not an
 * assumption about which reasons can exist: `formatReviewReason` falls back
 * to a readable humanization of any unrecognized code so a new reason added
 * to the backend later still renders sensibly (CLAUDE.md: production code
 * must never assume a fixed, closed set of fixture-derived values).
 */

const KNOWN_REVIEW_REASONS: Record<string, string> = {
  INHERITED_FINANCIAL_VALIDATION_REVIEW: "Inherited financial validation review",
  SUPPLIER_NAME_MISSING: "Supplier name missing",
  INVOICE_LINE_TOTAL_MISSING: "Invoice line total missing",
};

function humanizeReasonCode(code: string): string {
  return code
    .toLowerCase()
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export function formatReviewReason(code: string): string {
  return KNOWN_REVIEW_REASONS[code] ?? humanizeReasonCode(code);
}

/**
 * Reviewer reason-code presets (UI vocabulary only). The backend requires at
 * least one non-blank reason code for every decision and resume but does not
 * enumerate them, so these are a controlled, reviewable set matching the
 * boundary's `REASON_CODE_PATTERN`; they carry no policy of their own.
 */

export interface ReasonCodeOption {
  code: string;
  label: string;
}

export const APPROVE_REASON_CODES: ReasonCodeOption[] = [
  { code: "REVIEWER_VERIFIED", label: "Reviewer verified the invoice" },
  { code: "SUPPLIER_CONFIRMED", label: "Supplier confirmed" },
  { code: "PURCHASE_ORDER_CONFIRMED", label: "Purchase order confirmed" },
  { code: "TOTALS_VERIFIED", label: "Totals verified" },
];

export const CORRECT_REASON_CODES: ReasonCodeOption[] = [
  { code: "OCR_ERROR_CORRECTED", label: "Extraction error corrected" },
  { code: "MISSING_VALUE_SUPPLIED", label: "Missing value supplied" },
  { code: "EVIDENCE_VERIFIED", label: "Verified against evidence" },
];

export const REJECT_REASON_CODES: ReasonCodeOption[] = [
  { code: "INVALID_INVOICE", label: "Invalid invoice" },
  { code: "DUPLICATE_INVOICE", label: "Duplicate invoice" },
  { code: "WRONG_RECIPIENT", label: "Not addressed to this company" },
  { code: "SUSPECTED_FRAUD", label: "Suspected fraud" },
  { code: "OTHER", label: "Other (explain in notes)" },
];

export const RESUME_REASON_CODES: ReasonCodeOption[] = [
  { code: "REVIEW_COMPLETED", label: "Human review completed" },
];

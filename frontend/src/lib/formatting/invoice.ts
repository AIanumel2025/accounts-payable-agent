import { humanizeCode } from "@/lib/formatting/status";
import { MISSING_VALUE_DISPLAY, MISSING_VALUE_LABEL } from "@/lib/formatting/money";

/**
 * Invoice-field, confidence, assignment, audit-event and UUID-display
 * formatting (M11B task §12).
 */

const FIELD_NAME_LABELS: Record<string, string> = {
  SUPPLIER_NAME: "Supplier name",
  SUPPLIER_ADDRESS: "Supplier address",
  CUSTOMER_NAME: "Customer name",
  CUSTOMER_ADDRESS: "Customer address",
  INVOICE_NUMBER: "Invoice number",
  INVOICE_DATE: "Invoice date",
  DUE_DATE: "Due date",
  PURCHASE_ORDER_NUMBER: "Purchase order number",
  CURRENCY: "Currency",
  SUBTOTAL: "Subtotal",
  TAX_AMOUNT: "Tax amount",
  DISCOUNT_AMOUNT: "Discount amount",
  SHIPPING_AMOUNT: "Shipping amount",
  TOTAL_AMOUNT: "Total amount",
  PAYMENT_TERMS: "Payment terms",
  LINE_DESCRIPTION: "Line description",
  LINE_QUANTITY: "Line quantity",
  LINE_UNIT_PRICE: "Line unit price",
  LINE_AMOUNT: "Line amount",
};

export function fieldNameLabel(fieldName: string): string {
  return FIELD_NAME_LABELS[fieldName] ?? humanizeCode(fieldName);
}

const VALUE_TYPE_LABELS: Record<string, string> = {
  TEXT: "Text",
  DATE: "Date",
  DECIMAL: "Decimal",
  CURRENCY_CODE: "Currency code",
};

export function valueTypeLabel(valueType: string): string {
  return VALUE_TYPE_LABELS[valueType] ?? humanizeCode(valueType);
}

export interface FormattedConfidence {
  display: string;
  isMissing: boolean;
  isLow: boolean;
  ariaLabel: string;
}

const LOW_CONFIDENCE_THRESHOLD = 70;

/**
 * Confidence follows the backend contract: a percentage on a 0-100 scale
 * (99.99 -> "99.99%", 100 -> "100%"; M11E.4 -- it was wrongly treated as a
 * proportion and rendered 9999%), or `null` when the extractor never
 * produced one -- distinct from a low-but-present confidence (task §8B:
 * "distinguish low confidence from missing confidence").
 */
export function formatConfidence(confidence: number | null | undefined): FormattedConfidence {
  if (confidence === null || confidence === undefined) {
    return {
      display: MISSING_VALUE_DISPLAY,
      isMissing: true,
      isLow: false,
      ariaLabel: `Confidence ${MISSING_VALUE_LABEL.toLowerCase()}`,
    };
  }
  const percent = Number(confidence.toFixed(2)); // at most two decimals, no trailing zeros
  return {
    display: `${percent}%`,
    isMissing: false,
    isLow: confidence < LOW_CONFIDENCE_THRESHOLD,
    ariaLabel: `${percent}% confidence`,
  };
}

export interface FormattedAssignment {
  display: string;
  isUnassigned: boolean;
}

export function formatAssignment(assignedTo: string | null | undefined): FormattedAssignment {
  if (assignedTo === null || assignedTo === undefined || assignedTo.trim() === "") {
    return { display: "Unassigned", isUnassigned: true };
  }
  return { display: assignedTo, isUnassigned: false };
}

export interface FormattedIdentifier {
  /** Shortened for compact display -- the full value stays available via `full` for assistive technology (task §12). */
  display: string;
  full: string;
}

/** Shortens a UUID/hash for compact table display, e.g. "a1b2c3d4…" -- never the only place the full value is available. */
export function formatShortIdentifier(value: string, visibleChars = 8): FormattedIdentifier {
  if (value.length <= visibleChars) {
    return { display: value, full: value };
  }
  return { display: `${value.slice(0, visibleChars)}…`, full: value };
}

/**
 * `TimelineEventResponse.event_type` is a plain `str` in the FastAPI schema
 * (`src/ap_agent/api/schemas.py`), but every value that ever actually
 * reaches it is `ap_agent.repositories.mapping.domain_to_audit_event_row`'s
 * `event.event_type.value` -- i.e. a `MemoryEventType` member
 * (`src/ap_agent/models/memory.py`). This map covers that real, closed set
 * (M11B task §4: "never invent parameter names or enum values") --
 * `humanizeCode` is still the fallback for any future addition to the enum.
 */
const AUDIT_EVENT_TYPE_LABELS: Record<string, string> = {
  MEMORY_CREATED: "Memory record created",
  WORKFLOW_TRANSITIONED: "Workflow transitioned",
  PHASE_RESULT_LINKED: "Phase result linked",
  REVIEW_DECISION_RECORDED: "Review decision recorded",
  ARTIFACT_VERIFIED: "Artifact verified",
  MEMORY_CONFLICT_REJECTED: "Memory conflict rejected",
};

export function auditEventTypeLabel(eventType: string): string {
  return AUDIT_EVENT_TYPE_LABELS[eventType] ?? humanizeCode(eventType);
}

import type { CommandCapabilitiesPayload } from "@/types/api-payloads";

export const CASE_ID = "11111111-2222-4333-8444-555555555555";
export const COMMAND_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

export function makeCapabilities(overrides: Partial<CommandCapabilitiesPayload> = {}): CommandCapabilitiesPayload {
  return {
    command_mode: "COMMIT",
    actor_role: "AP_REVIEWER",
    case_status: "OPEN",
    assignment: "UNASSIGNED",
    review_revision: 1,
    workflow_revision: 1,
    permitted_actions: ["CLAIM", "RELEASE", "ACCEPT", "CORRECT", "REJECT", "RESUME_WORKFLOW"],
    available_actions: [
      { action: "CLAIM", disposition: null, requires_reason_codes: false, requires_notes: false, requires_corrections: false },
    ],
    unsupported_actions: ["CONFIRM_SUPPLIER", "CONFIRM_PURCHASE_ORDER", "REQUEST_INFORMATION", "ESCALATE"],
    correction_policy: {
      header_fields: [
        { field_name: "PURCHASE_ORDER_NUMBER", current_value: "99" },
        { field_name: "TOTAL_AMOUNT", current_value: "100.10" },
      ],
      line_fields: ["LINE_DESCRIPTION", "LINE_QUANTITY", "LINE_UNIT_PRICE", "LINE_AMOUNT"],
      line_numbers: [1],
      line_values: [
        { line_number: 1, field_name: "LINE_QUANTITY", current_value: "5" },
        { line_number: 1, field_name: "LINE_DESCRIPTION", current_value: "Widget" },
      ],
      evidence_reference_ids: ["ev-po-1", "ev-total-1"],
      evidence_options: [
        { reference_id: "ev-po-1", evidence_type: "EXTRACTED_FIELD", label: "Extracted evidence — Purchase Order Number, page 1", page_number: 1, snippet: "PO 99" },
        { reference_id: "ev-total-1", evidence_type: "EXTRACTED_FIELD", label: "Extracted evidence — Total Amount", page_number: null, snippet: null },
      ],
      require_reason: true,
      require_evidence: true,
    },
    resume: { eligible: false, already_requested: false, disposition: null, decision_id: null, ineligible_reason: "REVIEW_DECISION_REQUIRED" },
    payment_execution: "PROHIBITED",
    ...overrides,
  };
}

/** A case the calling actor has claimed and may decide. */
export function claimedCapabilities(overrides: Partial<CommandCapabilitiesPayload> = {}): CommandCapabilitiesPayload {
  return makeCapabilities({
    case_status: "IN_REVIEW",
    assignment: "ASSIGNED_TO_ACTOR",
    workflow_revision: 2,
    available_actions: (["RELEASE", "ACCEPT", "CORRECT", "REJECT"] as const).map((action) => ({
      action,
      disposition: action === "ACCEPT" ? "APPROVED" : action === "CORRECT" ? "CORRECTED" : action === "REJECT" ? "REJECTED" : null,
      requires_reason_codes: action !== "RELEASE",
      requires_notes: action === "REJECT",
      requires_corrections: action === "CORRECT",
    })),
    ...overrides,
  });
}

export function resumeCapabilities(disposition: "APPROVED" | "CORRECTED", overrides: Partial<CommandCapabilitiesPayload> = {}): CommandCapabilitiesPayload {
  return makeCapabilities({
    case_status: "RESOLVED",
    assignment: "ASSIGNED_TO_ACTOR",
    review_revision: 2,
    workflow_revision: 3,
    available_actions: [
      { action: "RESUME_WORKFLOW", disposition, requires_reason_codes: true, requires_notes: false, requires_corrections: false },
    ],
    resume: { eligible: true, already_requested: false, disposition, decision_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd", ineligible_reason: null },
    ...overrides,
  });
}

export function executedEnvelope(overrides: Record<string, unknown> = {}) {
  return {
    request_id: "99999999-9999-4999-8999-999999999999",
    status: "ACCEPTED",
    data: {
      command_id: COMMAND_ID,
      idempotency_key: "ap-ui-key-0001",
      status: "ACCEPTED",
      review_case_id: CASE_ID,
      document_id: "22222222-2222-4222-8222-222222222222",
      resulting_case_status: "IN_REVIEW",
      resulting_revision: 1,
      workflow_resumed: false,
      decision_id: null,
      message: "Human-review case claimed by reviewer.",
      ...overrides,
    },
    errors: [],
    generated_at: "2026-01-01T00:00:00Z",
  };
}

export function validatedEnvelope() {
  return {
    request_id: "88888888-8888-4888-8888-888888888888",
    status: "VALIDATED",
    data: {
      command_id: COMMAND_ID,
      review_case_id: CASE_ID,
      action: "CLAIM",
      command_fingerprint: "f".repeat(64),
      execution_mode: "VALIDATION_ONLY",
      database_mutation: false,
    },
    errors: [],
    generated_at: "2026-01-01T00:00:00Z",
  };
}

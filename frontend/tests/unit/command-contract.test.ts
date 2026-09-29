import { describe, expect, it } from "vitest";
import {
  ACTION_DISPOSITIONS,
  ACTION_LABELS,
  DEFERRED_ACTIONS,
  SUPPORTED_ACTIONS,
  parseBrowserCommandRequest,
  parseCommandFailure,
  parseCommandSuccess,
} from "@/lib/commands/contract";
import { COMMAND_ID, executedEnvelope, validatedEnvelope } from "../support/command-fixtures";

const base = () => ({
  command_id: COMMAND_ID,
  idempotency_key: "ap-ui-" + COMMAND_ID,
  action: "CLAIM",
  disposition: null,
  observed_review_revision: 1,
  observed_workflow_revision: 1,
  reason_codes: [],
  notes: null,
  corrections: [],
});

const correction = () => ({
  field_name: "PURCHASE_ORDER_NUMBER",
  line_number: null,
  previous_value: "99",
  corrected_value: "99A",
  reason: "Verified",
  evidence_reference_ids: ["ev-po-1"],
});

describe("supported actions (task §2/§16)", () => {
  it("contains exactly the six transactional actions", () => {
    expect([...SUPPORTED_ACTIONS]).toEqual(["CLAIM", "RELEASE", "ACCEPT", "CORRECT", "REJECT", "RESUME_WORKFLOW"]);
  });
  it("keeps the four deferred actions out of the supported set", () => {
    for (const action of DEFERRED_ACTIONS) expect(SUPPORTED_ACTIONS).not.toContain(action);
  });
  it("labels ACCEPT as Approve but keeps the wire value", () => {
    expect(ACTION_LABELS.ACCEPT).toBe("Approve");
    expect(ACTION_DISPOSITIONS.ACCEPT).toBe("APPROVED");
  });
});

describe("browser command request allow-list (task §7/§22)", () => {
  it("accepts a valid claim", () => {
    const parsed = parseBrowserCommandRequest(base());
    expect(parsed.ok).toBe(true);
  });

  it.each(["CONFIRM_SUPPLIER", "CONFIRM_PURCHASE_ORDER", "REQUEST_INFORMATION", "ESCALATE", "EXECUTE_PAYMENT", "RELEASE_PAYMENT", "BANK_TRANSFER", "POST_TO_ERP", "", 7])(
    "rejects unsupported action %s",
    (action) => {
      const parsed = parseBrowserCommandRequest({ ...base(), action });
      expect(parsed).toEqual(expect.objectContaining({ ok: false }));
      if (!parsed.ok) expect(parsed.errors).toContain("REVIEW_ACTION_NOT_SUPPORTED");
    },
  );

  it.each([
    "tenant_id", "actor_id", "actor_role", "role", "requested_at", "authenticated_at", "command_mode",
    "payment_amount", "bank_account", "erp_posting", "headers", "X-Tenant-ID",
  ])("rejects the unsupported field %s", (field) => {
    const parsed = parseBrowserCommandRequest({ ...base(), [field]: "x" });
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.errors.some((code) => code.startsWith("UNSUPPORTED_FIELD"))).toBe(true);
  });

  it("rejects unsupported fields inside a correction", () => {
    const parsed = parseBrowserCommandRequest({
      ...base(), action: "CORRECT", disposition: "CORRECTED", reason_codes: ["X_1"], corrections: [{ ...correction(), account: "1" }],
    });
    expect(parsed.ok).toBe(false);
  });

  it("enforces the action/disposition pairing", () => {
    expect(parseBrowserCommandRequest({ ...base(), action: "ACCEPT", disposition: "APPROVED", reason_codes: ["REVIEWER_VERIFIED"] }).ok).toBe(true);
    expect(parseBrowserCommandRequest({ ...base(), action: "ACCEPT", disposition: "REJECTED", reason_codes: ["REVIEWER_VERIFIED"] }).ok).toBe(false);
    expect(parseBrowserCommandRequest({ ...base(), action: "CLAIM", disposition: "APPROVED" }).ok).toBe(false);
    expect(parseBrowserCommandRequest({ ...base(), action: "RESUME_WORKFLOW", disposition: "REJECTED", reason_codes: ["X_1"] }).ok).toBe(false);
    expect(parseBrowserCommandRequest({ ...base(), action: "RESUME_WORKFLOW", disposition: "CORRECTED", reason_codes: ["REVIEW_COMPLETED"] }).ok).toBe(true);
  });

  it("only allows corrections on CORRECT", () => {
    const parsed = parseBrowserCommandRequest({ ...base(), action: "ACCEPT", disposition: "APPROVED", reason_codes: ["REVIEWER_VERIFIED"], corrections: [correction()] });
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.errors).toContain("CORRECTIONS_NOT_ALLOWED");
  });

  it.each([
    ["command_id", "not-a-uuid"],
    ["idempotency_key", "short"],
    ["idempotency_key", "bad key with spaces!!"],
    ["observed_review_revision", 0],
    ["observed_review_revision", 1.5],
    ["observed_workflow_revision", "1"],
    ["reason_codes", ["lowercase"]],
    ["reason_codes", "OK_CODE"],
    ["notes", 5],
  ])("rejects an invalid %s", (field, value) => {
    expect(parseBrowserCommandRequest({ ...base(), [field]: value }).ok).toBe(false);
  });

  it("rejects non-object bodies and unknown enum values safely, without echoing input", () => {
    for (const body of [null, "x", 5, [], [base()]]) expect(parseBrowserCommandRequest(body).ok).toBe(false);
    const secret = "sk-secret-value";
    const parsed = parseBrowserCommandRequest({ ...base(), [secret]: 1, field_name: secret });
    expect(JSON.stringify(parsed)).not.toContain(secret);
  });

  it("validates corrections: field names, evidence ids and exact string values", () => {
    const body = (patch: object) => ({
      ...base(), action: "CORRECT", disposition: "CORRECTED", reason_codes: ["EVIDENCE_VERIFIED"], corrections: [{ ...correction(), ...patch }],
    });
    expect(parseBrowserCommandRequest(body({})).ok).toBe(true);
    expect(parseBrowserCommandRequest(body({ field_name: "ACCOUNT_NUMBER" })).ok).toBe(false);
    expect(parseBrowserCommandRequest(body({ evidence_reference_ids: ["../etc/passwd"] })).ok).toBe(false);
    expect(parseBrowserCommandRequest(body({ line_number: 0 })).ok).toBe(false);
    expect(parseBrowserCommandRequest(body({ corrected_value: "" })).ok).toBe(false);
    const decimal = parseBrowserCommandRequest(body({ field_name: "TOTAL_AMOUNT", previous_value: "100.10", corrected_value: "100.100" }));
    expect(decimal.ok && decimal.value.corrections[0]?.corrected_value).toBe("100.100"); // exact string preserved
  });
});

describe("response parsing (task §5: no unchecked assertions)", () => {
  it("parses a validation-only result", () => {
    const parsed = parseCommandSuccess(validatedEnvelope());
    expect(parsed?.data.kind).toBe("VALIDATED");
  });

  it("parses committed and idempotent results", () => {
    expect(parseCommandSuccess(executedEnvelope())?.data.kind).toBe("ACCEPTED");
    expect(parseCommandSuccess({ ...executedEnvelope(), status: "IDEMPOTENT" })?.data.kind).toBe("IDEMPOTENT");
  });

  it("parses a workflow-resume result with handoff identity", () => {
    const parsed = parseCommandSuccess(
      executedEnvelope({ workflow_resumed: true, restart_stage: "FINANCIAL_VALIDATION", derived_version: "human-review-derived-abc", resume_plan_id: "33333333-3333-4333-8333-333333333333" }),
    );
    expect(parsed?.data.kind).toBe("ACCEPTED");
    if (parsed?.data.kind === "ACCEPTED") {
      expect(parsed.data.resume).toEqual({ restartStage: "FINANCIAL_VALIDATION", derivedVersion: "human-review-derived-abc", resumePlanId: "33333333-3333-4333-8333-333333333333" });
    }
  });

  it.each([
    null, "x", {}, { ...executedEnvelope(), status: "ACCEPTED", data: null },
    { ...executedEnvelope(), status: "SURPRISE" },
    { ...validatedEnvelope(), data: { ...validatedEnvelope().data, database_mutation: true } },
    { ...validatedEnvelope(), data: { ...validatedEnvelope().data, execution_mode: "COMMIT" } },
    executedEnvelope({ workflow_resumed: "yes" }),
    executedEnvelope({ message: 5 }),
    executedEnvelope({ resulting_case_status: 4 }),
  ])("rejects a malformed success payload #%#", (payload) => {
    expect(parseCommandSuccess(payload)).toBeNull();
  });

  it("keeps only well-formed error codes and never raw upstream text", () => {
    const failure = parseCommandFailure({
      request_id: "99999999-9999-4999-8999-999999999999",
      errors: ["STALE_REVIEW_REVISION", "INVALID_FIELD:corrections.0.reason", "psycopg.OperationalError: could not connect to postgres://u:p@host/db", "/home/user/secret.py", 5, "STALE_REVIEW_REVISION"],
    });
    expect(failure.errors).toEqual(["STALE_REVIEW_REVISION", "INVALID_FIELD:corrections.0.reason"]);
    expect(failure.requestId).toBe("99999999-9999-4999-8999-999999999999");
    expect(parseCommandFailure({ request_id: "junk", errors: "x" })).toEqual({ requestId: null, errors: [] });
    expect(parseCommandFailure(null)).toEqual({ requestId: null, errors: [] });
  });
});

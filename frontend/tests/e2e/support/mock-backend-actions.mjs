#!/usr/bin/env node
// Deterministic, *stateful* stand-in for the M10 FastAPI backend, used only
// by the M11C mocked Playwright suite (`playwright.actions.config.ts`).
//
// It mirrors the observable command contract of the real backend
// (`src/ap_agent/api/routes/review_commands.py`, `services/review_*.py`,
// `services/workflow_resume.py`): the same role table, the same validation
// error codes and HTTP statuses, the same idempotency semantics, the same
// revision arithmetic (review revision = 1 + decisions; workflow revision
// bumps on claim/release/decision/resume) and the same restart-stage policy.
// The real behaviour is proven separately against PostgreSQL
// (`tests/api/test_m11c_review_actions_postgres.py` and
// `tests/e2e/real-actions.spec.ts`); this mock exists so browser behaviour
// can be tested deterministically, including races and outages that are
// awkward to provoke against a real database.
//
// Control endpoints (never reachable from a browser; only the test runner):
//   POST /__reset                 -> restore the initial state
//   POST /__control {mode}        -> normal|unavailable|timeout|malformed|database_unavailable
//   POST /__control {health_mode} -> COMMIT|VALIDATION_ONLY (what /health and capabilities report)
//   POST /__control {race: true}  -> another reviewer silently claims the target case just
//                                    before the next command is processed (a real-looking race)
//   GET  /__stats                 -> counts + the last forwarded command's headers/body

import { createHash, randomUUID } from "node:crypto";
import { createServer } from "node:http";

const PORT = Number(process.env.MOCK_BACKEND_PORT ?? 4322);
const TENANT_ID = "00000000-0000-0000-0000-000000000000";
const BATCH_ID = "aaaaaaaa-0000-4000-8000-0000000000b1";

const ROLE_PERMISSIONS = {
  AP_OPERATOR: ["CLAIM", "RELEASE", "CORRECT", "CONFIRM_SUPPLIER", "CONFIRM_PURCHASE_ORDER", "REQUEST_INFORMATION", "ESCALATE"],
  AP_REVIEWER: ["CLAIM", "RELEASE", "ACCEPT", "CORRECT", "CONFIRM_SUPPLIER", "CONFIRM_PURCHASE_ORDER", "REQUEST_INFORMATION", "ESCALATE", "REJECT", "RESUME_WORKFLOW"],
  TENANT_ADMIN: ["CLAIM", "RELEASE", "ACCEPT", "CORRECT", "CONFIRM_SUPPLIER", "CONFIRM_PURCHASE_ORDER", "REQUEST_INFORMATION", "ESCALATE", "REJECT", "RESUME_WORKFLOW"],
  READ_ONLY_AUDITOR: [],
};
const SUPPORTED = ["CLAIM", "RELEASE", "ACCEPT", "CORRECT", "REJECT", "RESUME_WORKFLOW"];
const UNSUPPORTED = ["CONFIRM_SUPPLIER", "CONFIRM_PURCHASE_ORDER", "REQUEST_INFORMATION", "ESCALATE"];
const DISPOSITIONS = { ACCEPT: "APPROVED", CORRECT: "CORRECTED", REJECT: "REJECTED" };
const HEADER_FIELDS = [
  "SUPPLIER_NAME", "SUPPLIER_ADDRESS", "CUSTOMER_NAME", "CUSTOMER_ADDRESS", "INVOICE_NUMBER", "INVOICE_DATE", "DUE_DATE",
  "PURCHASE_ORDER_NUMBER", "CURRENCY", "SUBTOTAL", "TAX_AMOUNT", "DISCOUNT_AMOUNT", "SHIPPING_AMOUNT", "TOTAL_AMOUNT", "PAYMENT_TERMS",
];
const LINE_FIELDS = ["LINE_DESCRIPTION", "LINE_QUANTITY", "LINE_UNIT_PRICE", "LINE_AMOUNT"];
const FINANCIAL = new Set(["INVOICE_DATE", "DUE_DATE", "CURRENCY", "SUBTOTAL", "TAX_AMOUNT", "DISCOUNT_AMOUNT", "SHIPPING_AMOUNT", "TOTAL_AMOUNT", ...LINE_FIELDS]);
const REFERENCE = new Set(["SUPPLIER_NAME", "SUPPLIER_ADDRESS", "PURCHASE_ORDER_NUMBER"]);
const KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/;

let control;
let cases;
let idempotency;
let stats;

function makeCase(n, sourceName, invoiceNumber, extra = {}) {
  const hex = String(n).padStart(2, "0");
  return {
    review_case_id: `1111111${n}-0000-4000-8000-0000000000${hex}`,
    workflow_id: `2222222${n}-0000-4000-8000-0000000000${hex}`,
    document_id: `3333333${n}-0000-4000-8000-0000000000${hex}`,
    source_name: sourceName,
    invoice_number: invoiceNumber,
    supplier_name: "Acme Supplies",
    currency: "EUR",
    total_amount: "100.10",
    po_number: "99",
    status: "OPEN", // OPEN | CLAIMED | RESOLVED | REJECTED
    assigned_to: null,
    workflow_revision: 1,
    workflow_status: "REVIEW_REQUIRED",
    stage: "HUMAN_REVIEW",
    review_required: true,
    decisions: [],
    events: [],
    resumed: false,
    lines: [{ line_number: 1, description: "Widget", quantity: "5", unit_price: "20.02", amount: "100.10" }],
    created_at: "2026-06-01T09:00:00Z",
    ...extra,
  };
}

function initialState() {
  control = { mode: "normal", healthMode: "COMMIT", race: false };
  idempotency = new Map();
  stats = { commandPosts: 0, lastCommand: null };
  cases = [
    makeCase(1, "claim-release.pdf", "INV-CLAIM-001"),
    makeCase(2, "approve-me.pdf", "INV-APPROVE-002"),
    makeCase(3, "correct-me.pdf", "INV-CORRECT-003"),
    makeCase(4, "reject-me.pdf", "INV-REJECT-004"),
    makeCase(5, "race-me.pdf", "INV-RACE-005"),
    makeCase(6, "someone-elses.pdf", "INV-OTHER-006", { status: "CLAIMED", assigned_to: "another-reviewer", workflow_revision: 2 }),
    makeCase(7, "approve-and-resume.pdf", "INV-RESUME-007"),
  ];
}
initialState();

const reviewRevision = (c) => 1 + c.decisions.length;
const apiStatus = (c) => ({ OPEN: "OPEN", CLAIMED: "IN_REVIEW", RESOLVED: "RESOLVED", REJECTED: "REJECTED" })[c.status];

function fieldsFor(c) {
  return [
    { field_name: "INVOICE_NUMBER", value: c.invoice_number, evidence: ["ev-invnum-1"] },
    { field_name: "PURCHASE_ORDER_NUMBER", value: c.po_number, evidence: ["ev-po-1"] },
    { field_name: "SUPPLIER_NAME", value: c.supplier_name, evidence: ["ev-supplier-1", "ev-supplier-2"] },
    { field_name: "TOTAL_AMOUNT", value: c.total_amount, evidence: ["ev-total-1", "ev-total-2"] },
  ];
}
const evidenceIds = (c) => [...new Set(fieldsFor(c).flatMap((f) => f.evidence))].sort();

function detailFor(c) {
  return {
    tenant_id: TENANT_ID,
    review_case_id: c.review_case_id,
    workflow_id: c.workflow_id,
    batch_id: BATCH_ID,
    document_id: c.document_id,
    source_name: c.source_name,
    source_document_sha256: "a".repeat(64),
    original_document_uri: null,
    workflow_status: c.workflow_status,
    current_stage: c.stage,
    case_status: apiStatus(c),
    review_revision: reviewRevision(c),
    review_required: c.review_required,
    review_reasons: ["SUPPLIER_NAME_MISSING"],
    fields: fieldsFor(c).map((f) => ({
      field_name: f.field_name, raw_value: f.value, normalized_value: f.value,
      value_type: f.field_name === "TOTAL_AMOUNT" ? "DECIMAL" : "TEXT", confidence: 90, review_required: false,
      evidence_reference_ids: f.evidence,
    })),
    financial_checks: [{
      check_id: "check-1", check_type: "TOTAL_RECONCILIATION", status: "REVIEW_REQUIRED", message: "Total reconciliation check.",
      expected_value: c.total_amount, observed_value: c.total_amount, evidence_reference_ids: ["ev-total-1"],
    }],
    line_matches: [{
      line_match_id: "line-match-1", invoice_line_number: 1, purchase_order_line_number: 1, description_status: "MATCHED",
      quantity_status: "MATCHED", unit_price_status: "MATCHED", line_total_status: "MATCHED", review_required: false, review_reasons: [],
    }],
    timeline: [
      { event_id: "evt-0", event_type: "MEMORY_CREATED", stage: "INGESTION", status: "SUCCEEDED", actor_id: null, message: "Memory Created", occurred_at: c.created_at },
      ...c.events,
    ],
    review_decisions: c.decisions.map((d) => ({
      decision_id: d.decision_id, reviewer_id: d.reviewer_id, disposition: d.disposition, reason_codes: d.reason_codes, notes: d.notes, decided_at: d.decided_at,
    })),
  };
}

function queueItem(c) {
  return {
    tenant_id: TENANT_ID, review_case_id: c.review_case_id, workflow_id: c.workflow_id, batch_id: BATCH_ID, document_id: c.document_id,
    source_name: c.source_name, invoice_number: c.invoice_number, supplier_name: c.supplier_name, currency: c.currency, total_amount: c.total_amount,
    workflow_status: c.workflow_status, current_stage: c.stage, case_status: apiStatus(c), priority: "NORMAL",
    review_reasons: ["SUPPLIER_NAME_MISSING"], supplier_status: "MATCHED", purchase_order_status: "MATCHED", financial_validation_status: "REVIEW_REQUIRED",
    assigned_reviewer_id: c.assigned_to, review_revision: reviewRevision(c), created_at: c.created_at, updated_at: new Date().toISOString(),
  };
}

function actorOf(req) {
  return { id: req.headers["x-actor-id"], role: req.headers["x-actor-role"] };
}

function resumeCapability(c, actor, writes) {
  const latest = c.decisions.at(-1) ?? null;
  const permitted = (ROLE_PERMISSIONS[actor.role] ?? []).includes("RESUME_WORKFLOW");
  let reason = null;
  if (!writes) reason = "COMMAND_MODE_VALIDATION_ONLY";
  else if (!permitted) reason = "ACTION_NOT_PERMITTED";
  else if (latest === null) reason = "REVIEW_DECISION_REQUIRED";
  else if (c.status !== "RESOLVED") reason = "RESOLVED_CASE_REQUIRED";
  else if (!["APPROVED", "CORRECTED"].includes(latest.disposition)) reason = "RESUME_DISPOSITION_INVALID";
  else if (latest.reviewer_id !== actor.id || c.assigned_to !== actor.id) reason = "DECISION_ACTOR_MISMATCH";
  else if (c.resumed) reason = "RESUME_ALREADY_REQUESTED";
  return {
    eligible: reason === null, already_requested: c.resumed, disposition: latest?.disposition ?? null,
    decision_id: latest?.decision_id ?? null, ineligible_reason: reason,
  };
}

function capabilitiesFor(c, actor) {
  const writes = control.healthMode === "COMMIT";
  const permitted = SUPPORTED.filter((a) => (ROLE_PERMISSIONS[actor.role] ?? []).includes(a));
  const assignment = c.assigned_to === null ? "UNASSIGNED" : c.assigned_to === actor.id ? "ASSIGNED_TO_ACTOR" : "ASSIGNED_TO_OTHER";
  const owned = c.status === "CLAIMED" && assignment === "ASSIGNED_TO_ACTOR";
  const resume = resumeCapability(c, actor, writes);
  const eligible = new Set();
  if (permitted.includes("CLAIM") && c.status === "OPEN" && assignment === "UNASSIGNED") eligible.add("CLAIM");
  if (owned) for (const a of ["RELEASE", "ACCEPT", "CORRECT", "REJECT"]) if (permitted.includes(a)) eligible.add(a);
  if (resume.eligible) eligible.add("RESUME_WORKFLOW");
  const line = c.lines[0];
  return {
    command_mode: control.healthMode,
    actor_role: actor.role,
    case_status: apiStatus(c),
    assignment,
    review_revision: reviewRevision(c),
    workflow_revision: c.workflow_revision,
    permitted_actions: permitted,
    available_actions: SUPPORTED.filter((a) => eligible.has(a)).map((a) => ({
      action: a, disposition: a === "RESUME_WORKFLOW" ? resume.disposition : (DISPOSITIONS[a] ?? null),
      requires_reason_codes: !["CLAIM", "RELEASE"].includes(a), requires_notes: a === "REJECT", requires_corrections: a === "CORRECT",
    })),
    unsupported_actions: UNSUPPORTED,
    correction_policy: {
      header_fields: HEADER_FIELDS.filter((f) => fieldsFor(c).some((x) => x.field_name === f)).map((f) => ({ field_name: f, current_value: fieldsFor(c).find((x) => x.field_name === f).value })),
      line_fields: LINE_FIELDS,
      line_numbers: c.lines.map((l) => l.line_number),
      line_values: c.lines.flatMap((l) => [
        { line_number: l.line_number, field_name: "LINE_AMOUNT", current_value: l.amount },
        { line_number: l.line_number, field_name: "LINE_DESCRIPTION", current_value: l.description },
        { line_number: l.line_number, field_name: "LINE_QUANTITY", current_value: l.quantity },
        { line_number: l.line_number, field_name: "LINE_UNIT_PRICE", current_value: l.unit_price },
      ]),
      evidence_reference_ids: evidenceIds(c),
      // labelled like the real backend: "Extracted evidence — <Field>" with a short snippet (here naming the id so specs can pick one)
      evidence_options: evidenceIds(c).map((id) => {
        const field = fieldsFor(c).find((f) => f.evidence.includes(id));
        const name = field.field_name.toLowerCase().split("_").map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");
        return { reference_id: id, evidence_type: "EXTRACTED_FIELD", label: `Extracted evidence \u2014 ${name}, page 1`, page_number: 1, snippet: `token ${id}` };
      }),
      require_reason: true,
      require_evidence: true,
    },
    resume,
    payment_execution: "PROHIBITED",
  };
}

// ------------------------------------------------------------
// Command semantics (mirrors validate_review_command + executors)
// ------------------------------------------------------------

function validate(c, actor, body) {
  const errors = [];
  const add = (code) => { if (!errors.includes(code)) errors.push(code); };
  if (!KEY_PATTERN.test(body.idempotency_key)) add("IDEMPOTENCY_KEY_INVALID");
  if (!(ROLE_PERMISSIONS[actor.role] ?? []).includes(body.action)) add("ACTION_NOT_PERMITTED");
  if (body.observed_review_revision !== reviewRevision(c)) add("STALE_REVIEW_REVISION");
  if (body.observed_workflow_revision !== c.workflow_revision) add("STALE_WORKFLOW_REVISION");

  if (body.action === "CLAIM") {
    if (c.status !== "OPEN") add("CASE_NOT_OPEN");
    if (c.assigned_to !== null) add("CASE_ALREADY_ASSIGNED");
  } else if (body.action !== "RESUME_WORKFLOW") {
    if (c.status !== "CLAIMED") add("CASE_NOT_CLAIMED");
    if (c.assigned_to !== actor.id) add("CASE_ASSIGNED_TO_DIFFERENT_REVIEWER");
  }

  const required = DISPOSITIONS[body.action];
  if (required !== undefined) {
    if (body.disposition == null) add("DISPOSITION_REQUIRED");
    else if (body.disposition !== required) add("DISPOSITION_MISMATCH");
  } else if (["CLAIM", "RELEASE"].includes(body.action) && body.disposition != null) add("DISPOSITION_NOT_ALLOWED");

  if (body.action !== "CLAIM" && body.action !== "RELEASE" && !body.reason_codes.some((r) => r.trim())) add("REASON_CODE_REQUIRED");
  if (body.action === "REJECT" && !(body.notes ?? "").trim()) add("NOTES_REQUIRED");
  if (body.action === "CORRECT") { if (body.corrections.length === 0) add("CORRECTIONS_REQUIRED"); }
  else if (body.corrections.length > 0) add("CORRECTIONS_NOT_ALLOWED");

  const headers = new Map(fieldsFor(c).map((f) => [f.field_name, f.value]));
  const evidence = new Set(evidenceIds(c));
  const seen = new Set();
  for (const corr of body.corrections) {
    const target = `${corr.field_name}:${corr.line_number ?? "h"}`;
    if (seen.has(target)) add("DUPLICATE_CORRECTION_TARGET");
    seen.add(target);
    if (HEADER_FIELDS.includes(corr.field_name)) {
      if (corr.line_number != null) add("HEADER_CORRECTION_LINE_NUMBER_PROHIBITED");
      if (!headers.has(corr.field_name)) add("HEADER_FIELD_NOT_PRESENT");
      else if ((corr.previous_value ?? null) !== headers.get(corr.field_name)) add("PREVIOUS_VALUE_MISMATCH");
    } else if (LINE_FIELDS.includes(corr.field_name)) {
      if (corr.line_number == null || corr.line_number < 1) add("LINE_CORRECTION_LINE_NUMBER_REQUIRED");
      else if (!c.lines.some((l) => l.line_number === corr.line_number)) add("UNKNOWN_INVOICE_LINE_NUMBER");
    } else add("CORRECTION_FIELD_NOT_ALLOWED");
    if (!corr.corrected_value.trim()) add("CORRECTED_VALUE_MISSING");
    if (corr.corrected_value === corr.previous_value) add("CORRECTED_VALUE_UNCHANGED");
    if (!corr.reason.trim()) add("CORRECTION_REASON_REQUIRED");
    if (corr.evidence_reference_ids.length === 0) add("CORRECTION_EVIDENCE_REQUIRED");
    if (!corr.evidence_reference_ids.every((id) => evidence.has(id))) add("UNKNOWN_EVIDENCE_REFERENCE");
  }
  return errors;
}

function statusForErrors(errors) {
  if (errors.some((e) => ["ACTION_NOT_PERMITTED", "CROSS_TENANT_COMMAND"].includes(e))) return 403;
  if (errors.some((e) => ["STALE_REVIEW_REVISION", "STALE_WORKFLOW_REVISION", "IDEMPOTENCY_KEY_CONTENT_CONFLICT"].includes(e))) return 409;
  if (errors.some((e) => ["CASE_ASSIGNED_TO_DIFFERENT_REVIEWER", "DECISION_ACTOR_MISMATCH", "CASE_NOT_OPEN", "CASE_ALREADY_ASSIGNED", "CASE_NOT_CLAIMED", "WORKFLOW_NOT_AWAITING_RESUME"].includes(e))) return 409;
  return 422;
}

function fingerprint(body, actor, caseId) {
  const { requested_at: _ignored, ...rest } = body;
  return createHash("sha256").update(JSON.stringify({ ...rest, actor, caseId, reason_codes: [...body.reason_codes].sort() })).digest("hex");
}

function pushEvent(c, type, actor, message, status = "ACCEPTED") {
  c.events.push({ event_id: `evt-${c.events.length + 1}`, event_type: type, stage: "HUMAN_REVIEW", status, actor_id: actor.id, message, occurred_at: new Date().toISOString() });
}

function restartStage(disposition, corrections) {
  if (disposition === "APPROVED") return "MEMORY_PERSISTENCE";
  const names = corrections.map((x) => x.field_name);
  if (names.some((n) => FINANCIAL.has(n))) return "FINANCIAL_VALIDATION";
  if (names.some((n) => REFERENCE.has(n))) return "REFERENCE_MATCHING";
  return "FINANCIAL_VALIDATION";
}

function executeCommand(c, actor, body) {
  const fp = fingerprint(body, actor, c.review_case_id);
  const stored = idempotency.get(body.idempotency_key);
  if (stored) {
    if (stored.fingerprint !== fp) return { status: 409, errors: ["IDEMPOTENCY_KEY_CONTENT_CONFLICT"] };
    return { status: 200, envelopeStatus: "IDEMPOTENT", data: { ...stored.data, status: "IDEMPOTENT", message: "Identical review command was already accepted." } };
  }

  if (body.action === "RESUME_WORKFLOW") return executeResume(c, actor, body, fp);

  const errors = validate(c, actor, body);
  if (errors.length > 0) return { status: statusForErrors(errors), errors };

  const base = { command_id: body.command_id, idempotency_key: body.idempotency_key, status: "ACCEPTED", review_case_id: c.review_case_id, document_id: c.document_id, workflow_resumed: false, decision_id: null };
  let data;

  if (body.action === "CLAIM" || body.action === "RELEASE") {
    const claim = body.action === "CLAIM";
    c.status = claim ? "CLAIMED" : "OPEN";
    c.assigned_to = claim ? actor.id : null;
    c.workflow_revision += 1;
    const message = claim ? "Human-review case claimed by reviewer." : "Human-review case released to the queue.";
    pushEvent(c, "INTERFACE_COMMAND_EXECUTED", actor, message);
    data = { ...base, resulting_case_status: claim ? "IN_REVIEW" : "OPEN", resulting_revision: reviewRevision(c), message };
  } else {
    const decisionId = randomUUID();
    const disposition = DISPOSITIONS[body.action];
    c.decisions.push({
      decision_id: decisionId, reviewer_id: actor.id, disposition, reason_codes: body.reason_codes, notes: body.notes,
      corrections: body.corrections, decided_at: new Date().toISOString(),
    });
    c.status = disposition === "REJECTED" ? "REJECTED" : "RESOLVED";
    c.workflow_revision += 1;
    pushEvent(c, "INTERFACE_COMMAND_EXECUTED", actor, `Human-review decision recorded: ${disposition}.`);
    data = { ...base, resulting_case_status: apiStatus(c), resulting_revision: reviewRevision(c), decision_id: decisionId, message: "Human-review decision recorded successfully." };
  }
  idempotency.set(body.idempotency_key, { fingerprint: fp, data });
  return { status: 200, envelopeStatus: "ACCEPTED", data };
}

function executeResume(c, actor, body, fp) {
  const errors = [];
  const add = (code) => { if (!errors.includes(code)) errors.push(code); };
  const latest = c.decisions.at(-1) ?? null;
  if (!(ROLE_PERMISSIONS[actor.role] ?? []).includes("RESUME_WORKFLOW")) add("ACTION_NOT_PERMITTED");
  if (c.status !== "RESOLVED") add("RESOLVED_CASE_REQUIRED");
  if (c.assigned_to !== actor.id) add("CASE_ASSIGNED_TO_DIFFERENT_REVIEWER");
  if (body.observed_review_revision !== reviewRevision(c)) add("STALE_REVIEW_REVISION");
  if (body.observed_workflow_revision !== c.workflow_revision) add("STALE_WORKFLOW_REVISION");
  if (!["APPROVED", "CORRECTED"].includes(body.disposition)) add("RESUME_DISPOSITION_INVALID");
  if (latest && body.disposition !== latest.disposition) add("DECISION_DISPOSITION_MISMATCH");
  if (body.corrections.length > 0) add("RESUME_CORRECTIONS_NOT_ALLOWED");
  if (!body.reason_codes.some((r) => r.trim())) add("REASON_CODE_REQUIRED");
  if (latest === null) add("REVIEW_DECISION_REQUIRED");
  else if (latest.reviewer_id !== actor.id) add("DECISION_ACTOR_MISMATCH");
  if (errors.length === 0 && c.resumed) add("WORKFLOW_NOT_AWAITING_RESUME");
  if (errors.length > 0) return { status: statusForErrors(errors), errors };

  const stage = restartStage(latest.disposition, latest.corrections ?? []);
  const digest = createHash("sha256").update(JSON.stringify(latest)).digest("hex");
  c.resumed = true;
  c.workflow_revision += 1;
  c.stage = stage;
  c.workflow_status = "IN_PROGRESS";
  c.review_required = false;
  pushEvent(c, "WORKFLOW_RESUME_REQUESTED", actor, "Human-review decision produced a workflow resume plan.", "IN_PROGRESS");
  const data = {
    command_id: body.command_id, idempotency_key: body.idempotency_key, status: "ACCEPTED", review_case_id: c.review_case_id, document_id: c.document_id,
    resulting_case_status: "RESOLVED", resulting_revision: reviewRevision(c), workflow_resumed: true, decision_id: latest.decision_id,
    message: "Workflow resume plan created successfully.", restart_stage: stage, derived_version: `human-review-derived-${digest.slice(0, 16)}`,
    resume_plan_id: randomUUID(),
  };
  idempotency.set(body.idempotency_key, { fingerprint: fp, data });
  return { status: 200, envelopeStatus: "ACCEPTED", data };
}

// ------------------------------------------------------------
// HTTP plumbing
// ------------------------------------------------------------

function envelope(data, status = "SUCCEEDED") {
  return { request_id: randomUUID(), status, data, errors: [], generated_at: new Date().toISOString() };
}
function errorBody(errors) {
  return { request_id: randomUUID(), errors, generated_at: new Date().toISOString() };
}
function sendJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(payload) });
  res.end(payload);
}
function readBody(req) {
  return new Promise((resolve) => {
    let raw = "";
    req.on("data", (chunk) => (raw += chunk));
    req.on("end", () => resolve(raw));
  });
}

const ALLOWED_COMMAND_KEYS = new Set([
  "command_id", "idempotency_key", "action", "disposition", "observed_review_revision", "observed_workflow_revision",
  "reason_codes", "notes", "corrections", "requested_at",
]);

function parseCommandBody(raw) {
  let body;
  try { body = JSON.parse(raw); } catch { return { errors: ["REQUEST_VALIDATION_FAILED"] }; }
  if (typeof body !== "object" || body === null) return { errors: ["REQUEST_VALIDATION_FAILED"] };
  const errors = [];
  for (const key of Object.keys(body)) if (!ALLOWED_COMMAND_KEYS.has(key)) errors.push(`INVALID_FIELD:${key}`);
  const known = ["CLAIM", "RELEASE", "ACCEPT", "CORRECT", "CONFIRM_SUPPLIER", "CONFIRM_PURCHASE_ORDER", "REQUEST_INFORMATION", "ESCALATE", "REJECT", "RESUME_WORKFLOW"];
  if (!known.includes(body.action)) errors.push("REVIEW_ACTION_NOT_SUPPORTED");
  if (errors.length > 0) return { errors: ["REQUEST_VALIDATION_FAILED", ...errors] };
  return {
    body: {
      ...body, reason_codes: body.reason_codes ?? [], notes: body.notes ?? null, corrections: body.corrections ?? [], disposition: body.disposition ?? null,
    },
  };
}

function dashboard() {
  const active = cases.filter((c) => c.status === "OPEN" || c.status === "CLAIMED");
  return {
    tenant_id: TENANT_ID, generated_at: new Date().toISOString(),
    total_invoices: cases.length + 1, processing_invoices: 0, completed_invoices: 1, review_required_invoices: cases.length, failed_invoices: 0,
    open_review_cases: active.length, unassigned_review_cases: active.filter((c) => c.assigned_to === null).length,
    review_reason_counts: [{ reason: "SUPPLIER_NAME_MISSING", count: active.length }],
  };
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", `http://127.0.0.1:${PORT}`);

  if (url.pathname === "/__reset" && req.method === "POST") { initialState(); sendJson(res, 200, { ok: true }); return; }
  if (url.pathname === "/__stats" && req.method === "GET") { sendJson(res, 200, stats); return; }
  if (url.pathname === "/__control" && req.method === "POST") {
    try {
      const parsed = JSON.parse(await readBody(req));
      if (parsed.mode !== undefined) control.mode = parsed.mode;
      if (parsed.health_mode !== undefined) control.healthMode = parsed.health_mode;
      if (parsed.race !== undefined) control.race = Boolean(parsed.race);
      sendJson(res, 200, control);
    } catch { sendJson(res, 400, { errors: ["INVALID_CONTROL"] }); }
    return;
  }

  if (url.pathname === "/health") {
    sendJson(res, 200, envelope({ service: "ap-agent-review-api", api_version: "0.1.0", command_mode: control.healthMode, payment_execution: "PROHIBITED" }));
    return;
  }

  if (!(req.headers["x-tenant-id"] && req.headers["x-actor-id"] && req.headers["x-actor-role"])) {
    sendJson(res, 401, errorBody(["TENANT_ID_MISSING"]));
    return;
  }
  if (control.mode === "unavailable") { req.socket.destroy(); return; }
  if (control.mode === "timeout") return;
  if (control.mode === "malformed") { res.writeHead(200, { "content-type": "application/json" }); res.end("{not valid json"); return; }

  const actor = actorOf(req);

  if (url.pathname === "/api/v1/dashboard") { sendJson(res, 200, envelope(dashboard())); return; }

  if (url.pathname === "/api/v1/review-cases" && req.method === "GET") {
    const status = url.searchParams.get("status");
    const wanted = status ?? null;
    const items = cases.filter((c) => (wanted === null ? c.status === "OPEN" || c.status === "CLAIMED" : apiStatus(c) === wanted)).map(queueItem);
    sendJson(res, 200, envelope({ items, pagination: { page: 1, page_size: 25, total_count: items.length, total_pages: 1 } }));
    return;
  }

  const match = /^\/api\/v1\/review-cases\/([0-9a-f-]{36})(\/command-capabilities|\/commands)?$/.exec(url.pathname);
  if (match) {
    const c = cases.find((x) => x.review_case_id === match[1]);
    if (!c) { sendJson(res, 404, errorBody(["REVIEW_CASE_NOT_FOUND"])); return; }

    if (match[2] === "/commands" && req.method === "POST") {
      stats.commandPosts += 1;
      const raw = await readBody(req);
      stats.lastCommand = {
        headers: { tenant: req.headers["x-tenant-id"], actor: req.headers["x-actor-id"], role: req.headers["x-actor-role"], authenticatedAt: req.headers["x-authenticated-at"], authorization: req.headers.authorization ?? null, cookie: req.headers.cookie ?? null },
        body: (() => { try { return JSON.parse(raw); } catch { return null; } })(),
      };

      if (control.mode === "database_unavailable") { sendJson(res, 503, errorBody(["DATABASE_UNAVAILABLE"])); return; }

      const parsed = parseCommandBody(raw);
      if (parsed.errors) { sendJson(res, 422, errorBody(parsed.errors)); return; }

      if (control.race) {
        // Another reviewer wins the claim just before this command is processed.
        control.race = false;
        if (c.status === "OPEN") { c.status = "CLAIMED"; c.assigned_to = "racing-reviewer"; c.workflow_revision += 1; pushEvent(c, "INTERFACE_COMMAND_EXECUTED", { id: "racing-reviewer" }, "Human-review case claimed by reviewer."); }
      }

      if (control.healthMode !== "COMMIT") {
        if (parsed.body.action === "RESUME_WORKFLOW") { sendJson(res, 422, errorBody(["RESUME_REQUIRES_RESOLVED_DECISION"])); return; }
        const errors = validate(c, actor, parsed.body);
        if (errors.length > 0) { sendJson(res, statusForErrors(errors), errorBody(errors)); return; }
        sendJson(res, 200, envelope({
          command_id: parsed.body.command_id, review_case_id: c.review_case_id, action: parsed.body.action,
          command_fingerprint: fingerprint(parsed.body, actor, c.review_case_id), execution_mode: "VALIDATION_ONLY", database_mutation: false,
        }, "VALIDATED"));
        return;
      }

      if (UNSUPPORTED.includes(parsed.body.action)) { sendJson(res, 422, errorBody(["REVIEW_ACTION_NOT_IMPLEMENTED"])); return; }
      const result = executeCommand(c, actor, parsed.body);
      if (result.errors) { sendJson(res, result.status, errorBody(result.errors)); return; }
      sendJson(res, 200, envelope(result.data, result.envelopeStatus));
      return;
    }

    if (match[2] === "/command-capabilities") { sendJson(res, 200, envelope(capabilitiesFor(c, actor))); return; }
    if (match[2] === undefined) { sendJson(res, 200, envelope(detailFor(c))); return; }
  }

  sendJson(res, 404, errorBody(["NOT_FOUND"]));
});

server.listen(PORT, "127.0.0.1", () => console.log(`mock-backend-actions listening on http://127.0.0.1:${PORT}`));

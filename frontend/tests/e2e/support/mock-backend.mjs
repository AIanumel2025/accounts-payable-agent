#!/usr/bin/env node
// Lightweight stand-in for the M10 FastAPI backend, used only by the
// standard (non-integration) Playwright suite (M11A/M11B task §17) so
// dashboard/queue/detail-rendering assertions are meaningful without a
// live PostgreSQL database. It serves the exact `ApiEnvelope` shape
// `src/ap_agent/api/schemas.py` defines and seeds the controlled-fixture
// baseline numbers documented in the task brief -- this is test
// infrastructure, not a second backend implementation: the real
// FastAPI+PostgreSQL acceptance path is a separate suite
// (`tests/e2e/real-integration.spec.ts`, `playwright.integration.config.ts`).
//
// A stateful control endpoint lets browser tests simulate backend failure
// scenarios even though the browser never talks to this server directly
// (only the Next.js server does): `POST /__control` with JSON
// `{ "mode": "unavailable" | "malformed" | "timeout" | "normal" }` sets the
// mode every subsequent response honors, until reset back to "normal".
// Real FastAPI responses always include a small artificial delay so the
// dashboard/queue/detail `loading.tsx` states are observable in tests,
// matching realistic network latency.

import { createServer } from "node:http";

const PORT = Number(process.env.MOCK_BACKEND_PORT ?? 4312);
const RESPONSE_DELAY_MS = 350;

let mode = "normal";

const DASHBOARD_DATA = {
  tenant_id: "00000000-0000-0000-0000-000000000000",
  generated_at: new Date().toISOString(),
  total_invoices: 4,
  processing_invoices: 0,
  completed_invoices: 1,
  review_required_invoices: 3,
  failed_invoices: 0,
  open_review_cases: 3,
  unassigned_review_cases: 3,
  review_reason_counts: [
    { reason: "INHERITED_FINANCIAL_VALIDATION_REVIEW", count: 3 },
    { reason: "SUPPLIER_NAME_MISSING", count: 2 },
    { reason: "INVOICE_LINE_TOTAL_MISSING", count: 1 },
  ],
};

const HEALTH_DATA = {
  service: "ap-agent-review-api",
  api_version: "0.1.0",
  command_mode: "VALIDATION_ONLY",
  payment_execution: "PROHIBITED",
};

// Three review-required queue items, modelled on the real controlled
// fixtures' own field values (tests/golden/phase_{4,5,6}_expected_results.json)
// -- not a byte-for-byte replay (this mock never needs exact evidence/field
// counts; only the real-integration acceptance path does), but recognisably
// the same three documents with the same review-reason distribution the
// M11A/M11B dashboard baseline documents (3/2/1).
const REVIEW_CASE_TEMPLATE1 = {
  review_case_id: "aaaaaaaa-0000-4000-8000-000000000001",
  workflow_id: "aaaaaaaa-0000-4000-8000-000000000011",
  batch_id: "aaaaaaaa-0000-4000-8000-0000000000b1",
  document_id: "aaaaaaaa-0000-4000-8000-000000000021",
  source_name: "Template1_Instance90.jpg",
  invoice_number: null,
  supplier_name: null,
  currency: "EUR",
  total_amount: "873.58",
  workflow_status: "REVIEW_REQUIRED",
  current_stage: "HUMAN_REVIEW",
  case_status: "OPEN",
  priority: "HIGH",
  review_reasons: ["SUPPLIER_NAME_MISSING", "INVOICE_LINE_TOTAL_MISSING", "INHERITED_FINANCIAL_VALIDATION_REVIEW"],
  supplier_status: "NOT_FOUND",
  purchase_order_status: "MATCHED",
  financial_validation_status: "REVIEW_REQUIRED",
  assigned_reviewer_id: null,
  review_revision: 1,
  created_at: "2026-06-01T09:00:00Z",
  updated_at: "2026-06-01T09:05:00Z",
};

const REVIEW_CASE_08181 = {
  review_case_id: "bbbbbbbb-0000-4000-8000-000000000002",
  workflow_id: "bbbbbbbb-0000-4000-8000-000000000012",
  batch_id: "aaaaaaaa-0000-4000-8000-0000000000b1",
  document_id: "bbbbbbbb-0000-4000-8000-000000000022",
  source_name: "08181_warped_document_perspective_shadow.jpg",
  invoice_number: "308044",
  supplier_name: "Snyder, Hammond and Anderson",
  currency: null,
  total_amount: null,
  workflow_status: "REVIEW_REQUIRED",
  current_stage: "HUMAN_REVIEW",
  case_status: "OPEN",
  priority: "NORMAL",
  review_reasons: ["INHERITED_FINANCIAL_VALIDATION_REVIEW"],
  supplier_status: "MATCHED",
  purchase_order_status: "NOT_REFERENCED",
  financial_validation_status: "REVIEW_REQUIRED",
  assigned_reviewer_id: null,
  review_revision: 1,
  created_at: "2026-06-01T09:10:00Z",
  updated_at: "2026-06-01T09:12:00Z",
};

const REVIEW_CASE_AARON_BERGMAN = {
  review_case_id: "cccccccc-0000-4000-8000-000000000003",
  workflow_id: "cccccccc-0000-4000-8000-000000000013",
  batch_id: "aaaaaaaa-0000-4000-8000-0000000000b1",
  document_id: "cccccccc-0000-4000-8000-000000000023",
  source_name: "invoice_Aaron Bergman_36258.pdf",
  invoice_number: "36258",
  supplier_name: null,
  currency: "USD",
  total_amount: "50.10",
  workflow_status: "REVIEW_REQUIRED",
  current_stage: "HUMAN_REVIEW",
  case_status: "IN_REVIEW",
  priority: "CRITICAL",
  review_reasons: ["SUPPLIER_NAME_MISSING", "INHERITED_FINANCIAL_VALIDATION_REVIEW"],
  supplier_status: "NOT_FOUND",
  purchase_order_status: "MATCHED",
  financial_validation_status: "REVIEW_REQUIRED",
  assigned_reviewer_id: "reviewer-1",
  review_revision: 2,
  created_at: "2026-06-01T09:15:00Z",
  updated_at: "2026-06-01T09:20:00Z",
};

const REVIEW_QUEUE_ITEMS = [REVIEW_CASE_AARON_BERGMAN, REVIEW_CASE_TEMPLATE1, REVIEW_CASE_08181];

const TENANT_ID = "00000000-0000-0000-0000-000000000000";

function detailFor(item) {
  return {
    tenant_id: TENANT_ID,
    review_case_id: item.review_case_id,
    workflow_id: item.workflow_id,
    batch_id: item.batch_id,
    document_id: item.document_id,
    source_name: item.source_name,
    source_document_sha256: "a".repeat(64),
    original_document_uri: null,
    workflow_status: item.workflow_status,
    current_stage: item.current_stage,
    case_status: item.case_status,
    review_revision: item.review_revision,
    review_required: true,
    review_reasons: item.review_reasons,
    fields: [
      {
        field_name: "SUPPLIER_NAME",
        raw_value: item.supplier_name,
        normalized_value: item.supplier_name,
        value_type: "TEXT",
        confidence: item.supplier_name ? 0.95 : null,
        review_required: item.supplier_name === null,
        evidence_reference_ids: item.supplier_name ? ["ev-supplier-1", "ev-supplier-2"] : [],
      },
      {
        field_name: "INVOICE_NUMBER",
        raw_value: item.invoice_number,
        normalized_value: item.invoice_number,
        value_type: "TEXT",
        confidence: item.invoice_number ? 0.98 : null,
        review_required: item.invoice_number === null,
        evidence_reference_ids: item.invoice_number ? ["ev-invnum-1"] : [],
      },
      {
        field_name: "TOTAL_AMOUNT",
        raw_value: item.total_amount,
        normalized_value: item.total_amount,
        value_type: "DECIMAL",
        confidence: item.total_amount ? 0.6 : null,
        review_required: item.total_amount === null,
        evidence_reference_ids: item.total_amount ? ["ev-total-1", "ev-total-2", "ev-total-3"] : [],
      },
    ],
    financial_checks: [
      {
        check_id: "check-1",
        check_type: "TOTAL_RECONCILIATION",
        status: item.financial_validation_status,
        message: "Total reconciliation check.",
        expected_value: item.total_amount,
        observed_value: item.total_amount,
        evidence_reference_ids: ["ev-total-1"],
      },
      {
        check_id: "check-2",
        check_type: "REQUIRED_FINANCIAL_FIELDS",
        status: "PASSED",
        message: "Required financial fields present.",
        expected_value: null,
        observed_value: null,
        evidence_reference_ids: [],
      },
    ],
    line_matches:
      item.review_case_id === REVIEW_CASE_08181.review_case_id
        ? []
        : [
            {
              line_match_id: "line-match-1",
              invoice_line_number: 1,
              purchase_order_line_number: 1,
              description_status: "MATCHED",
              quantity_status: "MATCHED",
              unit_price_status: "MATCHED",
              line_total_status: "REVIEW_REQUIRED",
              review_required: true,
              review_reasons: ["INVOICE_LINE_TOTAL_MISSING"],
            },
          ],
    // event_type values are the real `MemoryEventType` members
    // (`src/ap_agent/models/memory.py`) that `domain_to_audit_event_row`
    // actually writes -- not an invented "WORKFLOW_STARTED"/"REVIEW_REQUIRED"
    // pair (M11B task §4 finding, see `lib/formatting/invoice.ts`).
    timeline: [
      {
        event_id: "evt-1",
        event_type: "MEMORY_CREATED",
        stage: "INGESTION",
        status: "SUCCEEDED",
        actor_id: null,
        message: "Memory Created",
        occurred_at: item.created_at,
      },
      {
        event_id: "evt-2",
        event_type: "WORKFLOW_TRANSITIONED",
        stage: "HUMAN_REVIEW",
        status: "REVIEW_REQUIRED",
        actor_id: null,
        message: "Workflow Transitioned",
        occurred_at: item.updated_at,
      },
    ],
    review_decisions: [],
  };
}

const REVIEW_CASE_DETAILS = new Map(REVIEW_QUEUE_ITEMS.map((item) => [item.review_case_id, detailFor(item)]));

function envelope(data) {
  return {
    request_id: "00000000-0000-0000-0000-000000000001",
    status: "SUCCEEDED",
    data,
    errors: [],
    generated_at: new Date().toISOString(),
  };
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

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const SUPPORTED_STATUS_FILTERS = new Set(["OPEN", "IN_REVIEW", "RESOLVED", "REJECTED"]);
const PRIORITY_ORDER = { CRITICAL: 1, HIGH: 2, NORMAL: 3, LOW: 4 };

function handleReviewCasesList(url, res) {
  const page = Math.max(1, Number.parseInt(url.searchParams.get("page") ?? "1", 10) || 1);
  const pageSize = Math.max(1, Number.parseInt(url.searchParams.get("page_size") ?? "25", 10) || 25);
  const status = url.searchParams.get("status");
  const assignedTo = url.searchParams.get("assigned_to");
  const priority = url.searchParams.get("priority");
  const batchId = url.searchParams.get("batch_id");

  if (status !== null && !SUPPORTED_STATUS_FILTERS.has(status)) {
    sendJson(res, 500, { errors: ["INTERNAL_ERROR"], generated_at: new Date().toISOString() });
    return;
  }

  let items = REVIEW_QUEUE_ITEMS.slice().sort((a, b) => {
    const priorityDiff = PRIORITY_ORDER[a.priority] - PRIORITY_ORDER[b.priority];
    if (priorityDiff !== 0) return priorityDiff;
    return a.created_at.localeCompare(b.created_at);
  });

  if (status !== null) items = items.filter((item) => item.case_status === status);
  if (assignedTo !== null) items = items.filter((item) => item.assigned_reviewer_id === assignedTo);
  if (priority !== null) items = items.filter((item) => item.priority === priority);
  if (batchId !== null) items = items.filter((item) => item.batch_id === batchId);

  const totalCount = items.length;
  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
  const start = (page - 1) * pageSize;
  const pageItems = items.slice(start, start + pageSize);

  sendJson(
    res,
    200,
    envelope({
      items: pageItems.map((item) => ({ tenant_id: TENANT_ID, ...item })),
      pagination: { page, page_size: pageSize, total_count: totalCount, total_pages: totalPages },
    }),
  );
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", `http://127.0.0.1:${PORT}`);

  if (url.pathname === "/__control" && req.method === "POST") {
    const body = await readBody(req);
    try {
      const parsed = JSON.parse(body);
      if (["normal", "unavailable", "malformed", "timeout"].includes(parsed.mode)) {
        mode = parsed.mode;
        sendJson(res, 200, { mode });
        return;
      }
    } catch {
      // fall through to 400 below
    }
    sendJson(res, 400, { errors: ["INVALID_CONTROL_MODE"] });
    return;
  }

  const authHeaderPresent = Boolean(req.headers["x-tenant-id"] && req.headers["x-actor-id"] && req.headers["x-actor-role"]);
  if (!authHeaderPresent) {
    sendJson(res, 401, { errors: ["TENANT_ID_MISSING"], generated_at: new Date().toISOString() });
    return;
  }

  if (mode === "unavailable") {
    req.socket.destroy();
    return;
  }

  if (mode === "timeout") {
    // Never respond; the client-side fetch's own timeout will fire.
    return;
  }

  if (mode === "malformed") {
    await delay(RESPONSE_DELAY_MS);
    res.writeHead(200, { "content-type": "application/json" });
    res.end("{not valid json");
    return;
  }

  await delay(RESPONSE_DELAY_MS);

  if (url.pathname === "/health") {
    sendJson(res, 200, envelope(HEALTH_DATA));
    return;
  }

  if (url.pathname === "/api/v1/dashboard") {
    sendJson(res, 200, envelope(DASHBOARD_DATA));
    return;
  }

  if (url.pathname === "/api/v1/review-cases") {
    handleReviewCasesList(url, res);
    return;
  }

  const detailMatch = /^\/api\/v1\/review-cases\/([^/]+)$/.exec(url.pathname);
  if (detailMatch) {
    const caseId = detailMatch[1];
    const detail = REVIEW_CASE_DETAILS.get(caseId);
    if (!detail) {
      sendJson(res, 404, { errors: ["REVIEW_CASE_NOT_FOUND"], generated_at: new Date().toISOString() });
      return;
    }
    sendJson(res, 200, envelope(detail));
    return;
  }

  sendJson(res, 404, { errors: ["NOT_FOUND"], generated_at: new Date().toISOString() });
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`mock-backend listening on http://127.0.0.1:${PORT}`);
});

#!/usr/bin/env node
// Lightweight stand-in for the M10 FastAPI backend, used only by the
// standard (non-integration) Playwright suite (M11A task §14) so
// dashboard-rendering assertions are meaningful without a live PostgreSQL
// database. It serves the exact `ApiEnvelope` shape
// `src/ap_agent/api/schemas.py` defines and seeds the controlled-fixture
// baseline numbers documented in the task brief (§9) -- this is test
// infrastructure, not a second backend implementation: the real
// FastAPI+PostgreSQL acceptance path is a separate suite
// (`tests/e2e/real-integration.spec.ts`, `playwright.integration.config.ts`).
//
// A stateful control endpoint lets browser tests simulate backend failure
// scenarios even though the browser never talks to this server directly
// (only the Next.js server does): `POST /__control` with JSON
// `{ "mode": "unavailable" | "malformed" | "timeout" | "normal" }` sets the
// mode every subsequent `/health`/`/api/v1/dashboard` response honors,
// until reset back to "normal". Real FastAPI responses always include a
// small artificial delay so the dashboard's `loading.tsx` state is
// observable in tests, matching realistic network latency.

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

  sendJson(res, 404, { errors: ["NOT_FOUND"], generated_at: new Date().toISOString() });
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`mock-backend listening on http://127.0.0.1:${PORT}`);
});

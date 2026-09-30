#!/usr/bin/env node
// M11D Core local demonstration: FastAPI + ONE separate worker + Next.js against
// the isolated test database. Fail-closed: needs AP_AGENT_TEST_POSTGRES_DSN naming
// ap_agent_m8_test; a fresh tenant and least-privilege role are created; a temporary
// artifact root is used; operations and review-command writes are enabled only in
// this demo's child processes; every child is stopped on exit.
//
//   npm run demo:m11d                  # prints a localhost URL; Ctrl+C to stop
//   npm run demo:m11d -- --check       # start, upload + process an invoice, verify, stop
//   npm run demo:m11d -- --port 3100   # choose the web port
//
// Prototype header authentication -- NOT production authentication.

import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  FIXTURE_INVOICES_DIRECTORY, FRONTEND_ROOT, cleanupM11dTenants, findFreePort, requireTestDsn, runOnceAndWait, seedM11dTenant,
  startFastApi, startNext, startWorker, stopProcess, waitForHealthy,
} from "./lib/stack-utils.mjs";

const args = process.argv.slice(2);
const checkOnly = args.includes("--check");
const portFlag = args.indexOf("--port");
const requestedPort = portFlag >= 0 ? Number(args[portFlag + 1]) : null;
const ocrProvider = process.env.AP_AGENT_WORKER_OCR_PROVIDER ?? "tesseract";

const testDsn = requireTestDsn();
const children = [];
let tenantId = null;
let workDir = null;
let stopping = false;

async function shutdown() {
  if (stopping) return;
  stopping = true;
  console.log("\nStopping the demonstration...");
  for (const child of children.reverse()) await stopProcess(child, "demo process");
  if (workDir) rmSync(workDir, { recursive: true, force: true });
  if (tenantId) await cleanupM11dTenants(testDsn, [tenantId]);
  console.log("Demo stopped. No background process is left running.");
  console.log("Jobs, job events, decisions, audit events, invoice memory and derived versions are append-only/restricted by schema design and remain in the isolated test database (tenant-isolated, inert).");
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    shutdown().finally(() => process.exit(0));
  });
}

async function main() {
  if (!existsSync(path.join(FRONTEND_ROOT, ".next", "BUILD_ID"))) {
    console.log("No production build found; running `npm run build` first...");
    const code = await runOnceAndWait("npm", ["run", "build"], {
      cwd: FRONTEND_ROOT,
      env: {
        ...process.env,
        AP_AGENT_API_BASE_URL: "http://127.0.0.1:1", AP_AGENT_FRONTEND_AUTH_MODE: "development_headers",
        AP_AGENT_DEV_TENANT_ID: "00000000-0000-0000-0000-000000000000", AP_AGENT_DEV_ACTOR_ID: "build", AP_AGENT_DEV_ACTOR_ROLE: "TENANT_ADMIN",
      },
    });
    if (code !== 0) throw new Error("`npm run build` failed.");
  }

  console.log("Creating an isolated demonstration tenant and least-privilege role in ap_agent_m8_test...");
  const state = await seedM11dTenant(testDsn);
  tenantId = state.tenant_id;
  workDir = mkdtempSync(path.join(tmpdir(), "ap-agent-m11d-demo-"));
  const artifactRoot = path.join(workDir, "artifacts");

  const apiPort = await findFreePort();
  const webPort = requestedPort ?? (await findFreePort());

  children.push(startFastApi({ port: apiPort, runtimeDsn: state.runtime_dsn, writes: true, operations: true, artifactRoot }));
  await waitForHealthy(`http://127.0.0.1:${apiPort}/health`);
  children.push(startWorker({ runtimeDsn: state.runtime_dsn, tenantId, artifactRoot, ocrProvider }));
  children.push(startNext({ port: webPort, apiPort, tenantId, actorId: "demo-admin", role: "TENANT_ADMIN", mode: "commit", operations: true }));
  await waitForHealthy(`http://127.0.0.1:${webPort}/operations`);

  console.log("");
  console.log("M11D Core demonstration is ready (isolated tenant, TENANT_ADMIN development actor, separate worker).");
  console.log(`  Open: http://127.0.0.1:${webPort}/operations`);
  console.log("  Flow: Operations -> Upload invoice -> Process -> follow the job -> review case -> Claim -> Approve or Correct");
  console.log("        -> Request workflow resume -> the worker resumes from the recorded stage.");
  console.log(`  Worker OCR provider: ${ocrProvider} (${ocrProvider === "tesseract" ? "real Tesseract via the Phase 3 fallback router; results route to review by design" : "real PaddleOCR"}).`);
  console.log(`  Sample invoices: ${FIXTURE_INVOICES_DIRECTORY}`);
  console.log("  No payment, bank or ERP action exists anywhere. Prototype header authentication -- NOT production authentication; do not expose this publicly.");

  if (checkOnly) {
    const base = `http://127.0.0.1:${webPort}`;
    const page = await (await fetch(`${base}/operations`)).text();
    if (!page.includes("Upload an invoice")) throw new Error("The operations page did not render the upload control.");

    const file = path.join(FIXTURE_INVOICES_DIRECTORY, "Template1_Instance90.jpg");
    const { readFileSync } = await import("node:fs");
    const match = /csrfToken\\?":\\?"([0-9]+\.[A-Za-z0-9_-]+)/.exec(page);
    if (!match) throw new Error("No CSRF token on the operations page.");

    const form = new FormData();
    form.set("operation_id", crypto.randomUUID());
    form.set("file", new Blob([readFileSync(file)], { type: "image/jpeg" }), "Template1_Instance90.jpg");
    const submission = await fetch(`${base}/api/v1/operations/submissions`, { method: "POST", headers: { origin: base, "x-csrf-token": match[1] }, body: form });
    const submitted = await submission.json();
    if (submission.status !== 202 || submitted.data.job.status !== "QUEUED") throw new Error(`Submission was not accepted as queued (HTTP ${submission.status}).`);
    console.log("--check: upload accepted and queued; waiting for the worker...");

    const jobId = submitted.data.job.job_id;
    const deadline = Date.now() + 300_000;
    let status = "QUEUED";
    while (Date.now() < deadline && !["SUCCEEDED", "REVIEW_REQUIRED", "FAILED"].includes(status)) {
      await new Promise((resolve) => setTimeout(resolve, 1_000));
      const detail = await (await fetch(`${base}/api/backend/api/v1/operations/jobs/${jobId}`)).json();
      status = detail.data.job.status;
    }
    if (status !== "REVIEW_REQUIRED" && status !== "SUCCEEDED") throw new Error(`The worker ended the job as ${status}.`);
    console.log(`--check: the worker processed the job (${status}). Shutting down.`);
    await shutdown();
    return;
  }

  console.log("\nPress Ctrl+C to stop; the demo's removable rows and temporary files are deleted on exit.");
  await new Promise(() => {});
}

main().catch(async (error) => {
  console.error(error instanceof Error ? error.message : error);
  await shutdown();
  process.exit(1);
});

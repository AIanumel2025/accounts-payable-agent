#!/usr/bin/env node
// M11D Core real FastAPI + separate worker + PostgreSQL + Next.js acceptance run.
//
//   1. seed a fresh isolated tenant (and a second tenant) in ap_agent_m8_test
//   2. start FastAPI as the freshly created least-privilege role with operations
//      AND review-command writes enabled -- explicit, this child only
//   3. start ONE separate worker process (python -m ap_agent.worker), explicitly
//      enabled for this child only and scoped to the isolated tenant
//   4. start two Next.js servers (TENANT_ADMIN, and AP_REVIEWER for the role check)
//   5. run tests/e2e/real-operations.spec.ts in a real Chromium session
//   6. inspect PostgreSQL directly (scripts/manage_m11d_tenant.py verify)
//   7. stop everything and delete the removable rows
//
// The test DSN reaches only the Python helpers' environment; it is never
// logged, never passed on a command line, never given to Next.js or the worker.
// OCR provider: AP_AGENT_WORKER_OCR_PROVIDER (default "tesseract" -- real
// Tesseract through the existing Phase 3 fallback router; set "paddleocr"
// in an environment that has PaddleOCR).

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  FRONTEND_ROOT, FIXTURE_INVOICES_DIRECTORY, MANAGE_M11D_SCRIPT, PYTHON_BIN, REPO_ROOT, cleanupM11dTenants, findFreePort,
  requireTestDsn, runOnceAndWait, seedM11dTenant, spawnProcess, startFastApi, startNext, startWorker, stopProcess, waitForHealthy,
} from "./lib/stack-utils.mjs";

const testDsn = requireTestDsn();
const ocrProvider = process.env.AP_AGENT_WORKER_OCR_PROVIDER ?? "tesseract";

const processes = [];
let state;
let workDir;
let artifactRoot;

async function main() {
  state = await seedM11dTenant(testDsn, { otherTenant: true });
  console.log(`M11D tenant ${state.tenant_id} seeded; second tenant ${state.other_tenant_id}.`);

  workDir = mkdtempSync(path.join(tmpdir(), "ap-agent-m11d-run-"));
  artifactRoot = path.join(workDir, "artifacts");

  const [apiPort, adminPort, reviewerPort] = await Promise.all(Array.from({ length: 3 }, () => findFreePort()));

  console.log("Starting FastAPI (operations + review writes enabled, this process only) as the least-privilege runtime role...");
  processes.push(startFastApi({ port: apiPort, runtimeDsn: state.runtime_dsn, writes: true, operations: true, artifactRoot }));
  await waitForHealthy(`http://127.0.0.1:${apiPort}/health`);
  const health = await (await fetch(`http://127.0.0.1:${apiPort}/health`)).json();
  if (health.data.operations_mode !== "ENABLED" || health.data.command_mode !== "COMMIT") {
    throw new Error("FastAPI did not report the expected operations/command modes.");
  }

  console.log(`Starting the single worker (OCR provider: ${ocrProvider}) as a separate process...`);
  processes.push(startWorker({ runtimeDsn: state.runtime_dsn, tenantId: state.tenant_id, artifactRoot, ocrProvider }));

  const instances = [
    { port: adminPort, apiPort, tenantId: state.tenant_id, actorId: "real-admin", role: "TENANT_ADMIN", mode: "commit", operations: true },
    { port: reviewerPort, apiPort, tenantId: state.tenant_id, actorId: "real-reviewer", role: "AP_REVIEWER", mode: "commit", operations: true },
  ];
  for (const instance of instances) processes.push(startNext(instance));
  for (const instance of instances) await waitForHealthy(`http://127.0.0.1:${instance.port}/dashboard`);
  console.log("FastAPI, worker and two Next.js servers are up.");

  const stack = {
    urls: { admin: `http://127.0.0.1:${adminPort}`, reviewer: `http://127.0.0.1:${reviewerPort}` },
    api: `http://127.0.0.1:${apiPort}`,
    tenantId: state.tenant_id,
    otherTenantId: state.other_tenant_id,
    ocrProvider,
    fixtures: FIXTURE_INVOICES_DIRECTORY,
    artifactRoot, // used only to assert the path never reaches the browser
    runtimePassword: new URL(state.runtime_dsn).password,
  };

  console.log("Running the real-operations Playwright spec...");
  const playwright = spawnProcess("npx", ["playwright", "test", "--config=playwright.integration-operations.config.ts", ...process.argv.slice(2)], {
    cwd: FRONTEND_ROOT,
    env: { ...process.env, AP_AGENT_ACCEPTANCE_STACK: JSON.stringify(stack), AP_AGENT_TEST_POSTGRES_DSN: "" },
  });
  const playwrightExit = await new Promise((resolve) => playwright.once("exit", (code) => resolve(code ?? 1)));

  console.log("\nVerifying the resulting database state directly in PostgreSQL...");
  const statePath = path.join(workDir, "state.json");
  writeFileSync(statePath, JSON.stringify(state), { mode: 0o600 });
  const verifyExit = await runOnceAndWait(PYTHON_BIN, [MANAGE_M11D_SCRIPT, "verify", "--state", statePath, "--expect-resume"], {
    cwd: REPO_ROOT,
    env: { ...process.env },
  });

  process.exitCode = playwrightExit !== 0 || verifyExit !== 0 ? 1 : 0;
  console.log(`\nBrowser scenarios: ${playwrightExit === 0 ? "PASSED" : "FAILED"}. PostgreSQL verification: ${verifyExit === 0 ? "PASSED" : "FAILED"}.`);
}

async function shutdown() {
  console.log("Stopping Next.js, the worker and FastAPI...");
  for (const child of processes.reverse()) await stopProcess(child, "child process");
  if (workDir) rmSync(workDir, { recursive: true, force: true });
  if (state) await cleanupM11dTenants(testDsn, [state.tenant_id, state.other_tenant_id]);
}

main()
  .catch((error) => {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  })
  .finally(shutdown);

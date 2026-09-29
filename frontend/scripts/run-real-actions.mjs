#!/usr/bin/env node
// M11C real FastAPI + PostgreSQL + Next.js write-enabled acceptance run.
//
//   1. seed a fresh isolated acceptance tenant (+ a second tenant for the
//      cross-tenant checks) in ap_agent_m8_test, one review case per scenario
//   2. start FastAPI twice as the freshly created least-privilege role: one
//      write-enabled (COMMIT) and one validation-only, both explicit and only
//      for this isolated run
//   3. start five Next.js servers: reviewer A, reviewer B, read-only auditor,
//      a reviewer of the *other* tenant (all frontend `commit`), and a
//      reviewer in frontend `validation_only`
//   4. run tests/e2e/real-actions.spec.ts in real Chromium sessions
//   5. inspect PostgreSQL directly (scripts/manage_m11c_tenant.py verify)
//   6. stop everything and delete the tenants' removable rows
//
// The test DSN reaches only the Python helper's environment; it is never
// logged, never passed on a command line, never given to Next.js, and FastAPI
// runs as a different, least-privilege, per-run role.

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  FRONTEND_ROOT, MANAGE_TENANT_SCRIPT, PYTHON_BIN, REPO_ROOT, cleanupTenants, findFreePort, requireTestDsn, runOnceAndWait,
  seedTenant, spawnProcess, startFastApi, startNext, stopProcess, waitForHealthy,
} from "./lib/stack-utils.mjs";

const testDsn = requireTestDsn();
const REVIEWER_A = "real-reviewer-a";
const REVIEWER_B = "real-reviewer-b";

const processes = [];
let state;
let workDir;

async function main() {
  const seeded = await seedTenant(testDsn, "acceptance");
  state = seeded.state;
  console.log(`Acceptance tenant ${state.tenant_id} seeded (${Object.keys(state.cases).length} cases); second tenant ${state.other_tenant_id}.`);

  const [writePort, validationPort, aPort, bPort, auditorPort, otherPort, validationUiPort] = await Promise.all(
    Array.from({ length: 7 }, () => findFreePort()),
  );

  console.log("Starting FastAPI (write-enabled, then validation-only) as the least-privilege runtime role...");
  processes.push(startFastApi({ port: writePort, runtimeDsn: state.runtime_dsn, writes: true }));
  processes.push(startFastApi({ port: validationPort, runtimeDsn: state.runtime_dsn, writes: false }));
  await waitForHealthy(`http://127.0.0.1:${writePort}/health`);
  await waitForHealthy(`http://127.0.0.1:${validationPort}/health`);
  const health = await (await fetch(`http://127.0.0.1:${writePort}/health`)).json();
  const validationHealth = await (await fetch(`http://127.0.0.1:${validationPort}/health`)).json();
  if (health.data.command_mode !== "COMMIT" || validationHealth.data.command_mode !== "VALIDATION_ONLY") {
    throw new Error("FastAPI did not report the expected command modes.");
  }
  console.log("FastAPI healthy: one COMMIT, one VALIDATION_ONLY.");

  const instances = [
    { port: aPort, apiPort: writePort, tenantId: state.tenant_id, actorId: REVIEWER_A, role: "AP_REVIEWER", mode: "commit" },
    { port: bPort, apiPort: writePort, tenantId: state.tenant_id, actorId: REVIEWER_B, role: "AP_REVIEWER", mode: "commit" },
    { port: auditorPort, apiPort: writePort, tenantId: state.tenant_id, actorId: "real-auditor", role: "READ_ONLY_AUDITOR", mode: "commit" },
    { port: otherPort, apiPort: writePort, tenantId: state.other_tenant_id, actorId: "other-tenant-reviewer", role: "AP_REVIEWER", mode: "commit" },
    { port: validationUiPort, apiPort: validationPort, tenantId: state.tenant_id, actorId: REVIEWER_A, role: "AP_REVIEWER", mode: "validation_only" },
  ];
  for (const instance of instances) processes.push(startNext(instance));
  for (const instance of instances) await waitForHealthy(`http://127.0.0.1:${instance.port}/dashboard`);
  console.log("Five Next.js servers are up (commit x4, validation_only x1).");

  const stack = {
    urls: {
      a: `http://127.0.0.1:${aPort}`, b: `http://127.0.0.1:${bPort}`, auditor: `http://127.0.0.1:${auditorPort}`,
      otherTenant: `http://127.0.0.1:${otherPort}`, validation: `http://127.0.0.1:${validationUiPort}`,
    },
    api: { write: `http://127.0.0.1:${writePort}`, validation: `http://127.0.0.1:${validationPort}` },
    tenantId: state.tenant_id,
    otherTenantId: state.other_tenant_id,
    reviewers: { a: REVIEWER_A, b: REVIEWER_B },
    // Case ids are identifiers of seeded test rows, not secrets.
    cases: Object.fromEntries(Object.entries(state.cases).map(([key, value]) => [key, value.review_case_id])),
    otherCases: Object.fromEntries(Object.entries(state.other_cases).map(([key, value]) => [key, value.review_case_id])),
  };

  console.log("Running the real-actions Playwright spec...");
  const playwright = spawnProcess("npx", ["playwright", "test", "--config=playwright.integration-actions.config.ts", ...process.argv.slice(2)], {
    cwd: FRONTEND_ROOT,
    env: { ...process.env, AP_AGENT_ACCEPTANCE_STACK: JSON.stringify(stack), AP_AGENT_TEST_POSTGRES_DSN: "" },
  });
  const playwrightExit = await new Promise((resolve) => playwright.once("exit", (code) => resolve(code ?? 1)));

  // Direct PostgreSQL verification runs whether or not a browser assertion failed, so a
  // database-level defect is never masked by a UI failure (and vice versa).
  console.log("\nVerifying the resulting database state directly in PostgreSQL...");
  workDir = mkdtempSync(path.join(tmpdir(), "ap-agent-m11c-verify-"));
  const statePath = path.join(workDir, "state.json");
  writeFileSync(statePath, JSON.stringify(state), { mode: 0o600 });
  const verifyExit = await runOnceAndWait(PYTHON_BIN, [MANAGE_TENANT_SCRIPT, "verify", "--state", statePath], {
    cwd: REPO_ROOT,
    env: { ...process.env },
  });

  process.exitCode = playwrightExit !== 0 || verifyExit !== 0 ? 1 : 0;
  console.log(`\nBrowser scenarios: ${playwrightExit === 0 ? "PASSED" : "FAILED"}. PostgreSQL verification: ${verifyExit === 0 ? "PASSED" : "FAILED"}.`);
}

async function shutdown() {
  console.log("Stopping Next.js and FastAPI...");
  for (const child of processes.reverse()) await stopProcess(child, "child process");
  if (workDir) rmSync(workDir, { recursive: true, force: true });
  if (state) await cleanupTenants(testDsn, [state.tenant_id, state.other_tenant_id]);
}

main()
  .catch((error) => {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  })
  .finally(shutdown);

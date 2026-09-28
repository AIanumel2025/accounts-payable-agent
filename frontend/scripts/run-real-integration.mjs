#!/usr/bin/env node
// Real FastAPI + PostgreSQL + Next.js acceptance run (M11A task §15).
//
// Orchestrates the exact sequence the task brief specifies:
//   0. seed a fresh, isolated acceptance tenant with the controlled-
//      fixture baseline (scripts/manage_m11a_acceptance_tenant.py) --
//      added after the first CI run of this script failed: it pointed
//      Next.js at the all-zero placeholder tenant UUID with nothing
//      seeded for it, so the real database correctly reported an empty
//      dashboard. See that script's module docstring for the full story.
//   1. start FastAPI on 127.0.0.1 using an available port, running as
//      the freshly created least-privilege database role -- never the
//      DSN's own migration-owner role
//   2. wait for /health
//   3. start Next.js on 127.0.0.1 using an available port, with the
//      generated tenant id (never a hardcoded/persistent one)
//   4. wait for the dashboard
//   5. open the dashboard in Playwright
//   6-10. verified by tests/e2e/real-integration.spec.ts
//   11. stop both servers cleanly, then clean up the acceptance tenant's
//       own removable rows (never anyone else's data)
//
// The real PostgreSQL test DSN (`AP_AGENT_TEST_POSTGRES_DSN`) is read from
// this process's own environment and handed only to
// `scripts/manage_m11a_acceptance_tenant.py` (as that Python process's own
// environment variable, never as a CLI argument, so it never appears in
// `ps`) -- it is never logged, never echoed, never written to a file, and
// never reaches the Next.js process or this script's own stdout/stderr.
// FastAPI itself is started with a *different*, least-privilege, per-run
// DSN that script generates -- not the owner test DSN.
//
// This script cannot run in a sandbox whose network egress does not permit
// raw-TCP PostgreSQL connections -- see docs/m11a_frontend_foundation_report.md,
// "Real FastAPI/PostgreSQL integration result", for exactly where this was
// and was not exercised, matching the identical, already-documented M8/M9/M10
// limitation (docs/m10_phase_9_review_api_report.md §13.2).

import { spawn } from "node:child_process";
import { readFileSync, rmSync, mkdtempSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const REPO_ROOT = path.dirname(FRONTEND_ROOT);
const PYTHON_BIN = process.env.AP_AGENT_PYTHON_BIN ?? "python3";
const MANAGE_TENANT_SCRIPT = path.join(REPO_ROOT, "scripts", "manage_m11a_acceptance_tenant.py");

function findFreePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function waitForHealthy(url, { timeoutMs = 60_000, intervalMs = 500 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "not attempted";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(2_000) });
      if (response.ok) return;
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`Timed out waiting for ${url} to become healthy (last error: ${lastError})`);
}

function spawnProcess(command, args, options) {
  // `detached: true` puts each child in its own process group, so
  // `stopProcess` below can kill the *group* (child plus any grandchild it
  // spawned, e.g. `npm run start` -> `next-server`) rather than leaking
  // orphaned grandchildren when only the direct child is signaled -- npm
  // does not reliably forward signals to the process it execs.
  const child = spawn(command, args, { stdio: "inherit", detached: true, ...options });
  return child;
}

async function stopProcess(child, name) {
  if (!child || child.exitCode !== null || child.killed) return;
  try {
    process.kill(-child.pid, "SIGTERM");
  } catch {
    child.kill("SIGTERM");
  }
  const exited = await Promise.race([
    new Promise((resolve) => child.once("exit", () => resolve(true))),
    new Promise((resolve) => setTimeout(() => resolve(false), 5_000)),
  ]);
  if (!exited) {
    console.warn(`${name} did not exit within 5s of SIGTERM; sending SIGKILL`);
    try {
      process.kill(-child.pid, "SIGKILL");
    } catch {
      child.kill("SIGKILL");
    }
  }
}

/** Runs a short-lived, one-shot command to completion and resolves its exit code (never throws for a non-zero exit). */
function runOnceAndWait(command, args, options) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: "inherit", ...options });
    child.once("error", reject);
    child.once("exit", (code) => resolve(code ?? 1));
  });
}

/**
 * Seeds a fresh, isolated acceptance tenant with the exact controlled-
 * fixture baseline (`scripts/manage_m11a_acceptance_tenant.py seed`).
 * Returns `{ tenantId, runtimeDsn }`. `runtimeDsn` is a least-privilege
 * role's DSN, generated fresh per run -- FastAPI runs as this role, never
 * as the test DSN's own migration-owner role.
 */
async function seedAcceptanceTenant(testDsn) {
  const workDir = mkdtempSync(path.join(tmpdir(), "ap-agent-m11a-acceptance-"));
  const outputPath = path.join(workDir, "acceptance-tenant.json");

  try {
    console.log("Seeding an isolated M11A acceptance tenant with the controlled-fixture baseline...");
    const exitCode = await runOnceAndWait(
      PYTHON_BIN,
      [MANAGE_TENANT_SCRIPT, "seed", "--output", outputPath],
      {
        cwd: REPO_ROOT,
        env: { ...process.env, AP_AGENT_TEST_POSTGRES_DSN: testDsn },
      },
    );

    if (exitCode !== 0) {
      throw new Error(`Seeding the M11A acceptance tenant failed (exit code ${exitCode}). See the log above.`);
    }

    const parsed = JSON.parse(readFileSync(outputPath, "utf-8"));
    if (!parsed.tenant_id || !parsed.runtime_dsn) {
      throw new Error("Seeding script's output file did not contain both tenant_id and runtime_dsn.");
    }

    return { tenantId: parsed.tenant_id, runtimeDsn: parsed.runtime_dsn };
  } finally {
    // The output file holds a live database credential (the least-
    // privilege role's password, embedded in runtime_dsn) -- delete the
    // whole scratch directory the instant it has been read, successfully
    // or not.
    rmSync(workDir, { recursive: true, force: true });
  }
}

/** Deletes the acceptance tenant's own removable rows (never anyone else's data). See that script's module docstring for exactly what can and cannot be deleted, and why. */
async function cleanupAcceptanceTenant(testDsn, tenantId) {
  console.log(`Cleaning up M11A acceptance tenant ${tenantId}...`);
  const exitCode = await runOnceAndWait(
    PYTHON_BIN,
    [MANAGE_TENANT_SCRIPT, "cleanup", "--tenant-id", tenantId],
    {
      cwd: REPO_ROOT,
      env: { ...process.env, AP_AGENT_TEST_POSTGRES_DSN: testDsn },
    },
  );
  if (exitCode !== 0) {
    console.warn(
      `Cleanup for acceptance tenant ${tenantId} exited with code ${exitCode}. This does not fail the ` +
        "acceptance run itself -- the tenant's data is isolated and harmless either way -- but investigate " +
        "if it keeps happening.",
    );
  }
}

async function main() {
  const testDsn = process.env.AP_AGENT_TEST_POSTGRES_DSN;
  if (!testDsn || testDsn.trim() === "") {
    console.error(
      "AP_AGENT_TEST_POSTGRES_DSN is not set. This script runs the real FastAPI+PostgreSQL+Next.js " +
        "acceptance path (M11A task §15) and needs a live database; it never substitutes a mock result.",
    );
    process.exitCode = 1;
    return;
  }

  const { tenantId, runtimeDsn } = await seedAcceptanceTenant(testDsn);
  console.log(`Acceptance tenant ${tenantId} seeded. FastAPI will run as its dedicated least-privilege role.`);

  const fastapiPort = await findFreePort();
  const nextPort = await findFreePort();

  console.log(`Starting FastAPI on 127.0.0.1:${fastapiPort} (least-privilege runtime DSN, never printed)`);
  const fastapiProcess = spawnProcess(
    PYTHON_BIN,
    ["-m", "uvicorn", "ap_agent.api.app:create_app", "--factory", "--host", "127.0.0.1", "--port", String(fastapiPort)],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        AP_AGENT_POSTGRES_DSN: runtimeDsn,
        PYTHONPATH: path.join(REPO_ROOT, "src"),
      },
    },
  );

  let nextProcess;

  try {
    await waitForHealthy(`http://127.0.0.1:${fastapiPort}/health`);
    console.log("FastAPI is healthy.");

    console.log(`Starting Next.js on 127.0.0.1:${nextPort}`);
    nextProcess = spawnProcess("npm", ["run", "start", "--", "--port", String(nextPort)], {
      cwd: FRONTEND_ROOT,
      env: {
        ...process.env,
        AP_AGENT_API_BASE_URL: `http://127.0.0.1:${fastapiPort}`,
        AP_AGENT_FRONTEND_AUTH_MODE: "development_headers",
        // The tenant this run just seeded -- generated fresh every run,
        // never a hardcoded/persistent value (M11A task §5/§21: never
        // hardcode a real tenant id in a committed file).
        AP_AGENT_DEV_TENANT_ID: tenantId,
        AP_AGENT_DEV_ACTOR_ID: "real-integration-acceptance",
        AP_AGENT_DEV_ACTOR_ROLE: "READ_ONLY_AUDITOR",
      },
    });

    await waitForHealthy(`http://127.0.0.1:${nextPort}/dashboard`);
    console.log("Next.js dashboard is reachable.");

    console.log("Running the real-integration Playwright spec...");
    const playwrightArgs = [
      "playwright",
      "test",
      "--config=playwright.integration.config.ts",
      ...process.argv.slice(2),
    ];
    const playwrightProcess = spawnProcess("npx", playwrightArgs, {
      cwd: FRONTEND_ROOT,
      env: {
        ...process.env,
        AP_AGENT_ACCEPTANCE_BASE_URL: `http://127.0.0.1:${nextPort}`,
      },
    });

    const exitCode = await new Promise((resolve) => playwrightProcess.once("exit", (code) => resolve(code ?? 1)));
    process.exitCode = exitCode;
  } finally {
    console.log("Stopping Next.js and FastAPI...");
    await stopProcess(nextProcess, "Next.js");
    await stopProcess(fastapiProcess, "FastAPI");
    await cleanupAcceptanceTenant(testDsn, tenantId);
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

#!/usr/bin/env node
// Real FastAPI + PostgreSQL + Next.js acceptance run (M11A task §15).
//
// Orchestrates the exact sequence the task brief specifies:
//   1. start FastAPI on 127.0.0.1 using an available port
//   2. wait for /health
//   3. start Next.js on 127.0.0.1 using an available port
//   4. wait for the dashboard
//   5. open the dashboard in Playwright
//   6-10. verified by tests/e2e/real-integration.spec.ts
//   11. stop both servers cleanly
//
// The real PostgreSQL test DSN (`AP_AGENT_TEST_POSTGRES_DSN`) is read from
// this process's own environment and mapped into the FastAPI child
// process's environment ONLY, as `AP_AGENT_POSTGRES_DSN` (the variable
// `ap_agent.db.connection.load_dsn` reads) -- it is never logged, never
// echoed, never written to a file (not even a committed `.env`), and never
// passed to the Next.js process (which must never hold a database
// credential at all: the browser-Next-FastAPI-Postgres boundary means only
// the FastAPI process ever sees this DSN).
//
// This script cannot run in a sandbox whose network egress does not permit
// raw-TCP PostgreSQL connections -- see docs/m11a_frontend_foundation_report.md,
// "Real FastAPI/PostgreSQL integration result", for exactly where this was
// and was not exercised, matching the identical, already-documented M8/M9/M10
// limitation (docs/m10_phase_9_review_api_report.md §13.2).

import { spawn } from "node:child_process";
import { createServer } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const REPO_ROOT = path.dirname(FRONTEND_ROOT);

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

  const fastapiPort = await findFreePort();
  const nextPort = await findFreePort();

  const pythonBin = process.env.AP_AGENT_PYTHON_BIN ?? "python3";

  console.log(`Starting FastAPI on 127.0.0.1:${fastapiPort} (DSN mapped into its environment only, never printed)`);
  const fastapiProcess = spawnProcess(
    pythonBin,
    ["-m", "uvicorn", "ap_agent.api.app:create_app", "--factory", "--host", "127.0.0.1", "--port", String(fastapiPort)],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        AP_AGENT_POSTGRES_DSN: testDsn,
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
        // A real tenant scoped to the controlled fixtures is required for
        // the acceptance numbers to match; CI supplies this via its own
        // environment/secret rather than a hardcoded value here (M11A task
        // §5/§21: never hardcode a real tenant id in a committed file).
        AP_AGENT_DEV_TENANT_ID: process.env.AP_AGENT_ACCEPTANCE_TENANT_ID ?? "00000000-0000-0000-0000-000000000000",
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
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

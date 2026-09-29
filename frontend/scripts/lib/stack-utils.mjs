// Shared process/seed helpers for the M11C real-stack runners
// (`run-real-actions.mjs`, `demo-m11c.mjs`). Nothing here ever logs a DSN.

import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const FRONTEND_ROOT = path.dirname(path.dirname(path.dirname(fileURLToPath(import.meta.url))));
export const REPO_ROOT = path.dirname(FRONTEND_ROOT);
export const PYTHON_BIN = process.env.AP_AGENT_PYTHON_BIN ?? "python3";
export const MANAGE_TENANT_SCRIPT = path.join(REPO_ROOT, "scripts", "manage_m11c_tenant.py");
export const REQUIRED_DATABASE = "ap_agent_m8_test";

/** Fail closed unless `AP_AGENT_TEST_POSTGRES_DSN` is set and names exactly `ap_agent_m8_test`. Never prints the DSN. */
export function requireTestDsn() {
  const dsn = process.env.AP_AGENT_TEST_POSTGRES_DSN;
  if (!dsn || dsn.trim() === "") {
    console.error("AP_AGENT_TEST_POSTGRES_DSN is not set. This needs a live isolated test database and never substitutes a mock.");
    process.exit(1);
  }
  let databaseName = "";
  try {
    databaseName = decodeURIComponent(new URL(dsn).pathname.replace(/^\//, ""));
  } catch {
    databaseName = "";
  }
  if (databaseName !== REQUIRED_DATABASE) {
    console.error(`AP_AGENT_TEST_POSTGRES_DSN must target the database ${REQUIRED_DATABASE}. Refusing to run (fail-closed).`);
    process.exit(1);
  }
  return dsn;
}

export function findFreePort() {
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

export async function waitForHealthy(url, { timeoutMs = 90_000, intervalMs = 500 } = {}) {
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

/** Detached so a whole process group (e.g. `npm run start` -> `next-server`) can be stopped together. */
export function spawnProcess(command, args, options) {
  return spawn(command, args, { stdio: "inherit", detached: true, ...options });
}

export async function stopProcess(child, name) {
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

export function runOnceAndWait(command, args, options) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: "inherit", ...options });
    child.once("error", reject);
    child.once("exit", (code) => resolve(code ?? 1));
  });
}

/**
 * Seeds via `scripts/manage_m11c_tenant.py seed`. The state file holds a live
 * least-privilege credential, so it is read once and the scratch directory is
 * deleted immediately. Returns the parsed state (which the caller must not log).
 */
export async function seedTenant(testDsn, profile) {
  const workDir = mkdtempSync(path.join(tmpdir(), "ap-agent-m11c-"));
  const outputPath = path.join(workDir, "state.json");
  try {
    const exitCode = await runOnceAndWait(PYTHON_BIN, [MANAGE_TENANT_SCRIPT, "seed", "--profile", profile, "--output", outputPath], {
      cwd: REPO_ROOT,
      env: { ...process.env, AP_AGENT_TEST_POSTGRES_DSN: testDsn },
    });
    if (exitCode !== 0) throw new Error(`Seeding the M11C ${profile} tenant failed (exit code ${exitCode}).`);
    const state = JSON.parse(readFileSync(outputPath, "utf-8"));
    if (!state.tenant_id || !state.runtime_dsn) throw new Error("Seed output is missing tenant_id/runtime_dsn.");
    return { state, workDir: null };
  } finally {
    rmSync(workDir, { recursive: true, force: true });
  }
}

export async function cleanupTenants(testDsn, tenantIds) {
  const args = [MANAGE_TENANT_SCRIPT, "cleanup", ...tenantIds.flatMap((id) => ["--tenant-id", id])];
  const exitCode = await runOnceAndWait(PYTHON_BIN, args, { cwd: REPO_ROOT, env: { ...process.env, AP_AGENT_TEST_POSTGRES_DSN: testDsn } });
  if (exitCode !== 0) console.warn(`Cleanup exited with code ${exitCode}; the tenants' data is isolated and inert either way.`);
}

export function startFastApi({ port, runtimeDsn, writes }) {
  return spawnProcess(
    PYTHON_BIN,
    ["-m", "uvicorn", "ap_agent.api.app:create_app", "--factory", "--host", "127.0.0.1", "--port", String(port), "--log-level", "warning"],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        AP_AGENT_POSTGRES_DSN: runtimeDsn,
        PYTHONPATH: path.join(REPO_ROOT, "src"),
        // Writes are enabled explicitly, per process, only for the isolated test tenant (M11C task §30).
        AP_AGENT_ENABLE_REVIEW_COMMAND_WRITES: writes ? "true" : "false",
      },
    },
  );
}

export function startNext({ port, apiPort, tenantId, actorId, role, mode }) {
  return spawnProcess("npm", ["run", "start", "--", "--port", String(port)], {
    cwd: FRONTEND_ROOT,
    env: {
      ...process.env,
      AP_AGENT_API_BASE_URL: `http://127.0.0.1:${apiPort}`,
      AP_AGENT_FRONTEND_AUTH_MODE: "development_headers",
      AP_AGENT_DEV_TENANT_ID: tenantId,
      AP_AGENT_DEV_ACTOR_ID: actorId,
      AP_AGENT_DEV_ACTOR_ROLE: role,
      AP_AGENT_FRONTEND_REVIEW_COMMAND_MODE: mode,
      NEXT_TELEMETRY_DISABLED: "1",
    },
  });
}

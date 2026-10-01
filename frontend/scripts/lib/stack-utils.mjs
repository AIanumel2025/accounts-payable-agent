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
export const MANAGE_M11D_SCRIPT = path.join(REPO_ROOT, "scripts", "manage_m11d_tenant.py");
// Controlled reference data for the demo/acceptance worker. Test data only: production
// configures its own directory through AP_AGENT_REFERENCE_DATA_DIRECTORY.
export const REFERENCE_DATA_DIRECTORY = path.join(REPO_ROOT, "tests", "fixtures", "reference_data");
export const FIXTURE_INVOICES_DIRECTORY = path.join(REPO_ROOT, "tests", "fixtures", "invoices");
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

export function startFastApi({ port, runtimeDsn, writes, operations = false, artifactRoot = null }) {
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
        // M11D Core: operations are enabled explicitly, per process, only for an isolated run.
        AP_AGENT_ENABLE_OPERATIONS: operations ? "true" : "false",
        ...(operations && artifactRoot ? { AP_AGENT_ARTIFACT_ROOT: artifactRoot } : {}),
      },
    },
  );
}

export function startNext({ port, apiPort, tenantId, actorId, role, mode, operations = false }) {
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
      AP_AGENT_FRONTEND_OPERATIONS_MODE: operations ? "enabled" : "disabled",
      NEXT_TELEMETRY_DISABLED: "1",
    },
  });
}

/** The single M11D Core worker process (continuous polling), scoped to one isolated tenant. Explicitly enabled for this child only. */
export function startWorker({ runtimeDsn, tenantId, artifactRoot, ocrProvider }) {
  return spawnProcess(PYTHON_BIN, ["-m", "ap_agent.worker"], {
    cwd: REPO_ROOT,
    env: {
      ...process.env,
      AP_AGENT_POSTGRES_DSN: runtimeDsn,
      PYTHONPATH: path.join(REPO_ROOT, "src"),
      AP_AGENT_ENABLE_WORKER_EXECUTION: "true",
      AP_AGENT_ARTIFACT_ROOT: artifactRoot,
      AP_AGENT_REFERENCE_DATA_DIRECTORY: REFERENCE_DATA_DIRECTORY,
      AP_AGENT_WORKER_TENANT_ID: tenantId,
      AP_AGENT_WORKER_OCR_PROVIDER: ocrProvider,
      // The worker never needs the owner DSN.
      AP_AGENT_TEST_POSTGRES_DSN: "",
    },
  });
}

/** Seeds via `scripts/manage_m11d_tenant.py seed`; the state file holds a live least-privilege credential, so it is read once and deleted. */
export async function seedM11dTenant(testDsn, { otherTenant = false } = {}) {
  const workDir = mkdtempSync(path.join(tmpdir(), "ap-agent-m11d-"));
  const outputPath = path.join(workDir, "state.json");
  try {
    const args = [MANAGE_M11D_SCRIPT, "seed", "--output", outputPath, ...(otherTenant ? ["--other-tenant"] : [])];
    const exitCode = await runOnceAndWait(PYTHON_BIN, args, { cwd: REPO_ROOT, env: { ...process.env, AP_AGENT_TEST_POSTGRES_DSN: testDsn } });
    if (exitCode !== 0) throw new Error(`Seeding the M11D tenant failed (exit code ${exitCode}).`);
    const state = JSON.parse(readFileSync(outputPath, "utf-8"));
    if (!state.tenant_id || !state.runtime_dsn) throw new Error("Seed output is missing tenant_id/runtime_dsn.");
    return state;
  } finally {
    rmSync(workDir, { recursive: true, force: true });
  }
}

export async function cleanupM11dTenants(testDsn, tenantIds) {
  const args = [MANAGE_M11D_SCRIPT, "cleanup", ...tenantIds.flatMap((id) => ["--tenant-id", id])];
  const exitCode = await runOnceAndWait(PYTHON_BIN, args, { cwd: REPO_ROOT, env: { ...process.env, AP_AGENT_TEST_POSTGRES_DSN: testDsn } });
  if (exitCode !== 0) console.warn(`Cleanup exited with code ${exitCode}; the tenants' data is isolated and inert either way.`);
}

// ---------------------------------------------------------------------------
// M11E hosted-mode acceptance helpers: Clerk-style bearer authentication,
// S3-compatible object storage (a mocked S3 service, not a real R2 bucket) and
// the database identity mapping. Test infrastructure only.
// ---------------------------------------------------------------------------

export const MANAGE_IDENTITY_SCRIPT = path.join(REPO_ROOT, "scripts", "manage_identity_mappings.py");

/** Runs the administrative identity CLI with the owner DSN as the migration/admin DSN (env only, never argv). */
export async function runIdentityCli(testDsn, args) {
  const env = { ...process.env, AP_AGENT_POSTGRES_MIGRATION_DSN: testDsn };
  delete env.AP_AGENT_TEST_POSTGRES_DSN;
  const exitCode = await runOnceAndWait(PYTHON_BIN, [MANAGE_IDENTITY_SCRIPT, ...args], { cwd: REPO_ROOT, env });
  if (exitCode !== 0) throw new Error(`manage_identity_mappings ${args[0]} failed (exit code ${exitCode}).`);
}

/** A mocked S3 service (moto server) on localhost -- the S3 API the Cloudflare R2 adapter speaks. */
export function startMotoServer(port) {
  return spawnProcess(PYTHON_BIN, ["-m", "moto.server", "-H", "127.0.0.1", "-p", String(port)], {
    cwd: REPO_ROOT,
    env: { ...process.env, AWS_ACCESS_KEY_ID: "testing", AWS_SECRET_ACCESS_KEY: "testing" },
    stdio: "ignore",
  });
}

export async function createBucket(endpoint, bucket) {
  const code = await runOnceAndWait(
    PYTHON_BIN,
    ["-c", "import boto3,sys;boto3.client('s3',endpoint_url=sys.argv[1],region_name='us-east-1',aws_access_key_id='testing',aws_secret_access_key='testing').create_bucket(Bucket=sys.argv[2])", endpoint, bucket],
    { cwd: REPO_ROOT, env: { ...process.env, NO_PROXY: "127.0.0.1,localhost", no_proxy: "127.0.0.1,localhost" } },
  );
  if (code !== 0) throw new Error("Creating the test bucket failed.");
}

/** Lists every object key in the bucket as JSON on stdout (read by the runner, never printed with credentials). */
export function listBucketKeys(endpoint, bucket) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      PYTHON_BIN,
      ["-c", "import boto3,json,sys;c=boto3.client('s3',endpoint_url=sys.argv[1],region_name='us-east-1',aws_access_key_id='testing',aws_secret_access_key='testing');print(json.dumps([o['Key'] for o in c.list_objects_v2(Bucket=sys.argv[2]).get('Contents',[])]))", endpoint, bucket],
      { cwd: REPO_ROOT, env: { ...process.env, NO_PROXY: "127.0.0.1,localhost", no_proxy: "127.0.0.1,localhost" } },
    );
    let out = "";
    child.stdout.on("data", (chunk) => (out += chunk));
    child.once("error", reject);
    child.once("exit", (code) => (code === 0 ? resolve(JSON.parse(out)) : reject(new Error("Listing the test bucket failed."))));
  });
}

const NO_PROXY_ENV = { NO_PROXY: "127.0.0.1,localhost", no_proxy: "127.0.0.1,localhost" };

export function startHostedFastApi({ port, runtimeDsn, issuer, jwksUrl, authorizedParty, s3Endpoint, bucket }) {
  return spawnProcess(
    PYTHON_BIN,
    ["-m", "uvicorn", "ap_agent.api.app:create_app", "--factory", "--host", "127.0.0.1", "--port", String(port), "--log-level", "warning"],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        ...NO_PROXY_ENV,
        AP_AGENT_POSTGRES_DSN: runtimeDsn,
        PYTHONPATH: path.join(REPO_ROOT, "src"),
        // `test`, not `hosted`: plain-http localhost endpoints are allowed. Every other hosted rule applies.
        AP_AGENT_ENVIRONMENT: "test",
        AP_AGENT_AUTH_MODE: "clerk_jwt",
        AP_AGENT_CLERK_ISSUER: issuer,
        AP_AGENT_CLERK_JWKS_URL: jwksUrl,
        AP_AGENT_CLERK_AUTHORIZED_PARTIES: authorizedParty,
        AP_AGENT_ARTIFACT_STORAGE: "s3",
        AP_AGENT_S3_ENDPOINT_URL: s3Endpoint,
        AP_AGENT_S3_BUCKET: bucket,
        AP_AGENT_S3_ACCESS_KEY_ID: "testing",
        AP_AGENT_S3_SECRET_ACCESS_KEY: "testing",
        AP_AGENT_S3_REGION: "auto",
        AP_AGENT_ENABLE_REVIEW_COMMAND_WRITES: "true",
        AP_AGENT_ENABLE_OPERATIONS: "true",
        AP_AGENT_TEST_POSTGRES_DSN: "",
      },
    },
  );
}

export function startHostedWorker({ runtimeDsn, tenantId, s3Endpoint, bucket, ocrProvider, scratchDirectory }) {
  return spawnProcess(PYTHON_BIN, ["-m", "ap_agent.worker"], {
    cwd: REPO_ROOT,
    env: {
      ...process.env,
      ...NO_PROXY_ENV,
      AP_AGENT_POSTGRES_DSN: runtimeDsn,
      PYTHONPATH: path.join(REPO_ROOT, "src"),
      AP_AGENT_ENVIRONMENT: "test",
      AP_AGENT_ENABLE_WORKER_EXECUTION: "true",
      AP_AGENT_ARTIFACT_STORAGE: "s3",
      AP_AGENT_S3_ENDPOINT_URL: s3Endpoint,
      AP_AGENT_S3_BUCKET: bucket,
      AP_AGENT_S3_ACCESS_KEY_ID: "testing",
      AP_AGENT_S3_SECRET_ACCESS_KEY: "testing",
      AP_AGENT_S3_REGION: "auto",
      AP_AGENT_PHASE_ARTIFACT_ROOT: scratchDirectory,
      TMPDIR: scratchDirectory,
      AP_AGENT_REFERENCE_DATA_DIRECTORY: REFERENCE_DATA_DIRECTORY,
      AP_AGENT_WORKER_TENANT_ID: tenantId,
      AP_AGENT_WORKER_OCR_PROVIDER: ocrProvider,
      AP_AGENT_TEST_POSTGRES_DSN: "",
    },
  });
}

export function startHostedNext({ port, apiPort, publishableKey, secretKey, jwtKey, authorizedParty }) {
  return spawnProcess("npm", ["run", "start", "--", "--port", String(port)], {
    cwd: FRONTEND_ROOT,
    env: {
      ...process.env,
      ...NO_PROXY_ENV,
      AP_AGENT_API_BASE_URL: `http://127.0.0.1:${apiPort}`,
      AP_AGENT_FRONTEND_AUTH_MODE: "clerk_jwt",
      AP_AGENT_ENVIRONMENT: "test",
      NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: publishableKey,
      CLERK_SECRET_KEY: secretKey,
      CLERK_JWT_KEY: jwtKey,
      AP_AGENT_CLERK_AUTHORIZED_PARTIES: authorizedParty,
      AP_AGENT_FRONTEND_CSRF_SECRET: "hosted-acceptance-csrf-secret-0123456789",
      AP_AGENT_FRONTEND_REVIEW_COMMAND_MODE: "commit",
      AP_AGENT_FRONTEND_OPERATIONS_MODE: "enabled",
      AP_AGENT_BACKEND_TIMEOUT_MS: "25000",
      NEXT_TELEMETRY_DISABLED: "1",
      AP_AGENT_TEST_POSTGRES_DSN: "",
    },
  });
}

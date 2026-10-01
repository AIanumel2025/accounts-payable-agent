#!/usr/bin/env node
// M11E hosted-mode acceptance run (local stand-ins for the hosted providers).
//
//   1. seed two isolated tenants in ap_agent_m8_test (migrations incl. 0005, least-privilege role)
//   2. register identity mappings through the administrative CLI (organization A -> tenant A,
//      organization B -> tenant B; admin/operator/reviewer/auditor, one disabled, one unregistered)
//   3. start a mocked S3 service, a local JWKS endpoint, FastAPI (clerk_jwt + S3), ONE worker
//      (S3 + OCR) and Next.js (clerk_jwt) -- the same code paths the hosted deployment runs
//   4. run tests/e2e/hosted-workflow.spec.ts in real Chromium with test-only session cookies
//   5. inspect the object store (keys are tenant-scoped, no filenames) and PostgreSQL
//   6. stop everything and remove the removable rows
//
// What this does NOT prove: real Clerk sign-in, real Cloudflare R2, real Render. Those are
// validated in the hosted acceptance (docs/m11e_deployment_runbook.md).
// The test DSN reaches only the Python helpers' environment.

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { createClerkTestKit, fakeClerkKeys } from "./lib/clerk-test-kit.mjs";
import {
  FIXTURE_INVOICES_DIRECTORY, FRONTEND_ROOT, MANAGE_M11D_SCRIPT, PYTHON_BIN, REPO_ROOT, cleanupM11dTenants, createBucket, findFreePort,
  listBucketKeys, requireTestDsn, runIdentityCli, runOnceAndWait, seedM11dTenant, spawnProcess, startHostedFastApi, startHostedNext,
  startHostedWorker, startMotoServer, stopProcess, waitForHealthy,
} from "./lib/stack-utils.mjs";

const testDsn = requireTestDsn();
const ocrProvider = process.env.AP_AGENT_WORKER_OCR_PROVIDER ?? "tesseract";
const BUCKET = "ap-agent-hosted-acceptance";
const ISSUER = "https://clerk.accounts.example.test";

const processes = [];
const servers = [];
let state;
let workDir;

async function main() {
  state = await seedM11dTenant(testDsn, { otherTenant: true });
  console.log(`Tenants ${state.tenant_id} and ${state.other_tenant_id} seeded.`);

  const suffix = Math.random().toString(36).slice(2, 10);
  const orgA = `org_hosted_a_${suffix}`;
  const orgB = `org_hosted_b_${suffix}`;
  const users = {
    admin: `user_admin_${suffix}`, operator: `user_operator_${suffix}`, reviewer: `user_reviewer_${suffix}`,
    auditor: `user_auditor_${suffix}`, inactive: `user_inactive_${suffix}`, unmapped: `user_unmapped_${suffix}`, otherAdmin: `user_other_${suffix}`,
  };

  console.log("Registering identity mappings through the administrative CLI...");
  const registrations = [
    [state.tenant_id, orgA, users.admin, "TENANT_ADMIN"], [state.tenant_id, orgA, users.operator, "AP_OPERATOR"],
    [state.tenant_id, orgA, users.reviewer, "AP_REVIEWER"], [state.tenant_id, orgA, users.auditor, "READ_ONLY_AUDITOR"],
    [state.tenant_id, orgA, users.inactive, "AP_OPERATOR"], [state.other_tenant_id, orgB, users.otherAdmin, "TENANT_ADMIN"],
  ];
  for (const [tenant, org, user, role] of registrations) {
    await runIdentityCli(testDsn, ["register", "--tenant-id", tenant, "--org-id", org, "--user-id", user, "--role", role]);
  }
  await runIdentityCli(testDsn, ["disable", "--org-id", orgA, "--user-id", users.inactive]);

  workDir = mkdtempSync(path.join(tmpdir(), "ap-agent-m11e-run-"));
  const scratch = path.join(workDir, "scratch");
  const [apiPort, webPort, s3Port, jwksPort] = await Promise.all(Array.from({ length: 4 }, () => findFreePort()));
  const web = `http://127.0.0.1:${webPort}`;
  const s3Endpoint = `http://127.0.0.1:${s3Port}`;

  console.log("Starting the mocked S3 service...");
  processes.push(startMotoServer(s3Port));
  await waitForHealthy(`${s3Endpoint}/moto-api/`);
  await createBucket(s3Endpoint, BUCKET);

  const clerk = fakeClerkKeys();
  const kit = createClerkTestKit({ issuer: ISSUER, authorizedParty: web });
  servers.push(await kit.startJwksServer(jwksPort));

  console.log("Starting FastAPI (clerk_jwt + S3), the worker and Next.js (clerk_jwt)...");
  processes.push(startHostedFastApi({
    port: apiPort, runtimeDsn: state.runtime_dsn, issuer: ISSUER, jwksUrl: `http://127.0.0.1:${jwksPort}/.well-known/jwks.json`,
    authorizedParty: web, s3Endpoint, bucket: BUCKET,
  }));
  await waitForHealthy(`http://127.0.0.1:${apiPort}/health/ready`);

  processes.push(startHostedWorker({ runtimeDsn: state.runtime_dsn, tenantId: state.tenant_id, s3Endpoint, bucket: BUCKET, ocrProvider, scratchDirectory: scratch }));
  processes.push(startHostedNext({ port: webPort, apiPort, publishableKey: clerk.publishableKey, secretKey: clerk.secretKey, jwtKey: kit.publicPem, authorizedParty: web }));
  await waitForHealthy(`${web}/sign-in`);

  const token = (user, org, options = {}) => kit.mint({ user, org, ...options });
  const session = (value) => kit.sessionCookies(value, web);
  const stack = {
    web,
    api: `http://127.0.0.1:${apiPort}`,
    tenantId: state.tenant_id,
    otherTenantId: state.other_tenant_id,
    orgNames: { a: orgA, b: orgB },
    ocrProvider,
    fixtures: FIXTURE_INVOICES_DIRECTORY,
    // Test-only session cookies per persona (signed by the test key; they exist only in this run).
    cookies: {
      admin: session(token(users.admin, orgA)),
      operator: session(token(users.operator, orgA)),
      reviewer: session(token(users.reviewer, orgA)),
      auditor: session(token(users.auditor, orgA)),
      inactive: session(token(users.inactive, orgA)),
      unmapped: session(token(users.unmapped, orgA)),
      otherAdmin: session(token(users.otherAdmin, orgB)),
      noOrganization: session(token(users.operator, null)),
      wrongAuthorizedParty: session(token(users.operator, orgA, { azp: web.replace("127.0.0.1", "localhost") })),
      tampered: session(token(users.operator, orgA, { tamper: true })),
    },
    // Values that must never reach a browser, a screenshot or an artifact.
    secrets: {
      clerkSecretKey: clerk.secretKey, runtimePassword: new URL(state.runtime_dsn).password, s3Endpoint, bucket: BUCKET,
      apiHost: `127.0.0.1:${apiPort}`, jwksHost: `127.0.0.1:${jwksPort}`, s3Secret: "testing",
    },
  };

  if (process.env.AP_AGENT_HOSTED_HOLD === "1") {
    // Debug aid: keep the stack up (Ctrl+C or SIGTERM to stop). The stack file holds test-only cookies.
    const holdPath = path.join(workDir, "stack.json");
    writeFileSync(holdPath, JSON.stringify(stack), { mode: 0o600 });
    console.log(`Stack held; description in ${holdPath}`);
    await new Promise((resolve) => { process.once("SIGINT", resolve); process.once("SIGTERM", resolve); });
    return;
  }

  console.log("Running the hosted-workflow Playwright spec...");
  const playwright = spawnProcess("npx", ["playwright", "test", "--config=playwright.hosted.config.ts", ...process.argv.slice(2)], {
    cwd: FRONTEND_ROOT,
    env: { ...process.env, AP_AGENT_ACCEPTANCE_STACK: JSON.stringify(stack), AP_AGENT_TEST_POSTGRES_DSN: "" },
  });
  const playwrightExit = await new Promise((resolve) => playwright.once("exit", (code) => resolve(code ?? 1)));

  console.log("\nInspecting the object store...");
  const keys = await listBucketKeys(s3Endpoint, BUCKET);
  const problems = [];
  if (keys.length === 0) problems.push("no object was stored");
  for (const key of keys) {
    if (!new RegExp(`^tenants/${state.tenant_id.replaceAll("-", "")}/jobs/[0-9a-f]{32}/source/[0-9a-f]{64}$`).test(key)) problems.push(`unexpected object key shape: ${key}`);
    if (/\.(pdf|png|jpe?g)$/i.test(key) || /Template1|08181|invoice/i.test(key)) problems.push("an object key contains a file name");
  }
  if (keys.some((key) => key.includes(state.other_tenant_id.replaceAll("-", "")))) problems.push("an object was stored under the other tenant");
  console.log(problems.length === 0 ? `Object store: ${keys.length} object(s), all tenant-scoped, content-addressed, no file names. PASSED.` : `Object store problems: ${problems.join("; ")}`);

  console.log("Verifying the resulting database state directly in PostgreSQL...");
  const statePath = path.join(workDir, "state.json");
  writeFileSync(statePath, JSON.stringify(state), { mode: 0o600 });
  const verifyExit = await runOnceAndWait(PYTHON_BIN, [MANAGE_M11D_SCRIPT, "verify", "--state", statePath, "--expect-resume"], {
    cwd: REPO_ROOT,
    env: { ...process.env },
  });

  process.exitCode = playwrightExit !== 0 || verifyExit !== 0 || problems.length > 0 ? 1 : 0;
  console.log(`\nBrowser scenarios: ${playwrightExit === 0 ? "PASSED" : "FAILED"}. PostgreSQL verification: ${verifyExit === 0 ? "PASSED" : "FAILED"}. Object store: ${problems.length === 0 ? "PASSED" : "FAILED"}.`);
}

async function shutdown() {
  console.log("Stopping Next.js, the worker, FastAPI and the mocked S3 service...");
  for (const child of processes.reverse()) await stopProcess(child, "child process");
  for (const server of servers) server.close();
  if (workDir) rmSync(workDir, { recursive: true, force: true });
  if (state) await cleanupM11dTenants(testDsn, [state.tenant_id, state.other_tenant_id]);
}

main()
  .catch((error) => {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  })
  .finally(shutdown);

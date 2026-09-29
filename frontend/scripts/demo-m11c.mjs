#!/usr/bin/env node
// `npm run demo:m11c` -- a safe, locally explorable M11C demonstration.
//
//   - fails closed unless AP_AGENT_TEST_POSTGRES_DSN names ap_agent_m8_test
//   - seeds a FRESH isolated demo tenant (three real named fixtures plus a few
//     generic cases) and a dedicated least-privilege runtime role
//   - starts FastAPI in write-enabled mode as that role, for this tenant only
//   - starts Next.js in frontend `commit` mode as an AP_REVIEWER
//   - prints only the local browser URL and safe status lines (never a DSN
//     or password)
//   - on Ctrl+C / SIGTERM / exit: stops both servers and deletes the tenant's
//     removable rows (append-only rows remain by schema design)
//
// `--check` starts the stack, confirms the review-action workspace renders,
// then shuts down (used to smoke-test this launcher itself). `--port N`
// picks the browser port (default: a free one).
//
// Prototype header authentication is used for the demo actor: it is NOT
// production authentication and must never be exposed publicly.

import { existsSync } from "node:fs";
import path from "node:path";
import {
  FRONTEND_ROOT, cleanupTenants, findFreePort, requireTestDsn, runOnceAndWait, seedTenant, startFastApi, startNext, stopProcess, waitForHealthy,
} from "./lib/stack-utils.mjs";

const args = process.argv.slice(2);
const checkOnly = args.includes("--check");
const portFlag = args.indexOf("--port");
const requestedPort = portFlag >= 0 ? Number(args[portFlag + 1]) : null;

const testDsn = requireTestDsn();
const children = [];
let tenantId = null;
let stopping = false;

async function shutdown() {
  if (stopping) return;
  stopping = true;
  console.log("\nStopping the demonstration...");
  for (const child of children.reverse()) await stopProcess(child, "demo process");
  if (tenantId) await cleanupTenants(testDsn, [tenantId]);
  console.log("Demo stopped. No background process is left running.");
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
        AP_AGENT_DEV_TENANT_ID: "00000000-0000-0000-0000-000000000000", AP_AGENT_DEV_ACTOR_ID: "build", AP_AGENT_DEV_ACTOR_ROLE: "AP_REVIEWER",
      },
    });
    if (code !== 0) throw new Error("`npm run build` failed.");
  }

  console.log("Seeding an isolated demonstration tenant in ap_agent_m8_test...");
  const { state } = await seedTenant(testDsn, "demo");
  tenantId = state.tenant_id;

  const apiPort = await findFreePort();
  const webPort = requestedPort ?? (await findFreePort());

  children.push(startFastApi({ port: apiPort, runtimeDsn: state.runtime_dsn, writes: true }));
  await waitForHealthy(`http://127.0.0.1:${apiPort}/health`);

  children.push(
    startNext({ port: webPort, apiPort, tenantId, actorId: "demo-reviewer", role: "AP_REVIEWER", mode: "commit" }),
  );
  await waitForHealthy(`http://127.0.0.1:${webPort}/dashboard`);

  const firstCase = Object.values(state.cases)[0];
  console.log("");
  console.log("M11C demonstration is ready (write-enabled, isolated tenant, AP_REVIEWER development actor).");
  console.log(`  Open:  http://127.0.0.1:${webPort}/dashboard`);
  console.log(`  Queue: http://127.0.0.1:${webPort}/review-queue`);
  console.log("  Workflow: Dashboard -> Review queue -> open an invoice -> Claim -> Correct or Approve -> Request workflow resume.");
  console.log("  Workflow resume creates a controlled handoff only; downstream processing does not run.");
  console.log("  Prototype header authentication -- NOT production authentication; do not expose this publicly.");

  if (checkOnly) {
    const page = await (await fetch(`http://127.0.0.1:${webPort}/review-cases/${firstCase.review_case_id}`)).text();
    if (!page.includes("Review actions")) throw new Error("The review-action workspace did not render.");
    console.log("--check: the review-action workspace rendered. Shutting down.");
    await shutdown();
    return;
  }

  console.log("\nPress Ctrl+C to stop; the demo tenant's removable rows are deleted on exit.");
  await new Promise(() => {});
}

main().catch(async (error) => {
  console.error(error instanceof Error ? error.message : error);
  await shutdown();
  process.exit(1);
});

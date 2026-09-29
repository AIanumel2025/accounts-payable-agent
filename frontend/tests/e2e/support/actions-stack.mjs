#!/usr/bin/env node
// Starts the whole M11C mocked-actions stack for `playwright.actions.config.ts`:
//   - the stateful mock backend (port 4322)
//   - four Next.js servers sharing one production build, differing only in
//     their server-only env (command mode + actor role):
//       4321  commit            AP_REVIEWER
//       4323  validation_only   AP_REVIEWER
//       4324  disabled          AP_REVIEWER
//       4325  commit            READ_ONLY_AUDITOR
//   - a tiny "ready" server (port 4320) that only answers once all of the
//     above respond, so Playwright's single `webServer.url` gates on all.
// Set AP_AGENT_SKIP_BUILD=1 to reuse an existing `.next` build.

import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(path.dirname(path.dirname(path.dirname(fileURLToPath(import.meta.url)))));
const MOCK_PORT = 4322;
const BACKEND = `http://127.0.0.1:${MOCK_PORT}`;
const TENANT = "00000000-0000-0000-0000-000000000000";

const INSTANCES = [
  { port: 4321, mode: "commit", role: "AP_REVIEWER" },
  { port: 4323, mode: "validation_only", role: "AP_REVIEWER" },
  { port: 4324, mode: "disabled", role: "AP_REVIEWER" },
  { port: 4325, mode: "commit", role: "READ_ONLY_AUDITOR" },
];

const children = [];
function stopAll() {
  for (const child of children) {
    try { process.kill(-child.pid, "SIGTERM"); } catch { try { child.kill("SIGTERM"); } catch { /* already gone */ } }
  }
}
process.on("SIGTERM", () => { stopAll(); process.exit(0); });
process.on("SIGINT", () => { stopAll(); process.exit(0); });
process.on("exit", stopAll);

function start(command, args, env) {
  // stdio "ignore": children must not hold Playwright's webServer pipes open, or its teardown would wait on them forever.
  const child = spawn(command, args, { cwd: ROOT, env: { ...process.env, ...env }, stdio: "ignore", detached: true });
  children.push(child);
  return child;
}

async function waitFor(url, timeoutMs = 240_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(2_000) });
      if (response.status < 500) return;
    } catch { /* not up yet */ }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  throw new Error(`Timed out waiting for ${url}`);
}

if (process.env.AP_AGENT_SKIP_BUILD !== "1" || !existsSync(path.join(ROOT, ".next", "BUILD_ID"))) {
  const build = spawnSync("npm", ["run", "build"], { cwd: ROOT, stdio: "inherit", env: { ...process.env, AP_AGENT_API_BASE_URL: BACKEND, AP_AGENT_FRONTEND_AUTH_MODE: "development_headers", AP_AGENT_DEV_TENANT_ID: TENANT, AP_AGENT_DEV_ACTOR_ID: "build", AP_AGENT_DEV_ACTOR_ROLE: "AP_REVIEWER" } });
  if (build.status !== 0) process.exit(build.status ?? 1);
}

start("node", ["tests/e2e/support/mock-backend-actions.mjs"], { MOCK_BACKEND_PORT: String(MOCK_PORT) });
await waitFor(`${BACKEND}/health`);

for (const { port, mode, role } of INSTANCES) {
  start("npx", ["next", "start", "--port", String(port)], {
    AP_AGENT_API_BASE_URL: BACKEND,
    AP_AGENT_FRONTEND_AUTH_MODE: "development_headers",
    AP_AGENT_DEV_TENANT_ID: TENANT,
    AP_AGENT_DEV_ACTOR_ID: "reviewer-1",
    AP_AGENT_DEV_ACTOR_ROLE: role,
    AP_AGENT_FRONTEND_REVIEW_COMMAND_MODE: mode,
    AP_AGENT_BACKEND_TIMEOUT_MS: "1500",
    NEXT_TELEMETRY_DISABLED: "1",
  });
}
for (const { port } of INSTANCES) await waitFor(`http://127.0.0.1:${port}/dashboard`);

createServer((_req, res) => { res.writeHead(200); res.end("ready"); }).listen(4320, "127.0.0.1", () => console.log("actions stack ready"));

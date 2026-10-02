#!/usr/bin/env node
// Unauthenticated smoke test of a deployed web service (M11E). Needs no credentials.
//
//   HOSTED_BASE_URL=https://<web-host> node scripts/hosted-smoke.mjs
//   (optional) HOSTED_R2_OBJECT_URL=https://<account>.r2.cloudflarestorage.com/<bucket>/<key>  -> must NOT be readable
//
// Verifies the public surface fails closed and leaks nothing. The authenticated workflow
// (upload, worker, review, resume) needs real sign-ins and is the manual part of
// docs/m11e_deployment_runbook.md section 9.

const base = (process.env.HOSTED_BASE_URL ?? "").replace(/\/$/, "");
if (!/^https:\/\/[^/]+$/.test(base)) {
  console.error("Set HOSTED_BASE_URL to the https origin of the web service.");
  process.exit(2);
}

const failures = [];
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok || !detail ? "" : ` (${detail})`}`);
  if (!ok) failures.push(name);
};
const get = (path, init = {}) => fetch(`${base}${path}`, { redirect: "manual", signal: AbortSignal.timeout(20_000), ...init });

const signIn = await get("/sign-in");
check("GET /sign-in is reachable", signIn.status === 200, `HTTP ${signIn.status}`);
const html = await signIn.text();

for (const path of ["/dashboard", "/operations", "/review-queue"]) {
  const response = await get(path);
  const location = response.headers.get("location") ?? "";
  check(`GET ${path} redirects an anonymous visitor to sign-in`, [302, 303, 307].includes(response.status) && /\/sign-in/.test(location), `HTTP ${response.status} ${location.split("?")[0]}`);
}

const api = await get("/api/backend/api/v1/dashboard");
check("anonymous API read answers 401 JSON", api.status === 401 && /AUTHENTICATION_REQUIRED/.test(await api.text()), `HTTP ${api.status}`);

const upload = await get("/api/v1/operations/submissions", { method: "POST", headers: { origin: base }, body: new FormData() });
check("anonymous upload is refused (401)", upload.status === 401, `HTTP ${upload.status}`);

check("no X-Powered-By header", !signIn.headers.has("x-powered-by"));

const leaks = [/sk_(live|test)_[A-Za-z0-9]{8,}/, /postgres(ql)?:\/\/[^\s"'<>]+:[^\s"'<>]+@/, /AP_AGENT_[A-Z_]+\s*=/, /CLERK_SECRET_KEY/, /r2\.cloudflarestorage\.com/, /BEGIN (RSA )?PRIVATE KEY/];
check("sign-in page HTML contains no secret-shaped value", leaks.every((pattern) => !pattern.test(html)));

if (process.env.HOSTED_R2_OBJECT_URL) {
  const object = await fetch(process.env.HOSTED_R2_OBJECT_URL, { redirect: "manual", signal: AbortSignal.timeout(20_000) });
  check("stored object is not publicly readable", [400, 401, 403, 404].includes(object.status), `HTTP ${object.status}`);
}

if (failures.length > 0) {
  console.error(`\n${failures.length} check(s) failed.`);
  process.exit(1);
}
console.log("\nUnauthenticated hosted smoke test passed. Continue with the authenticated checks in the runbook.");

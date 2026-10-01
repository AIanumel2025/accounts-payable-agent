# M11E — Authentication, Cloud Storage and Hosted Deployment

Status: **PR open, not merged. Repository work complete and verified locally; NOT deployed.**
Provider accounts/secrets (Clerk, Cloudflare R2, Render) were not available to this session, so no hosted
deployment, real Clerk sign-in, real R2 object or Render deploy has been exercised. See §8 for the exact
setup checklist.

Starting commit `44859dd` (M11D Core, merged). Branch: `claude/amazing-euler-546zxj` — the branch assigned
by the execution harness, **not** `claude/m11e-hosted-mvp` (deviation D-1). The working tree was clean at the start.

## 1. Verification status (read this first)

| Level | Status |
|---|---|
| **Locally verified** | Backend: full suite (local PostgreSQL 16) — see §6. Frontend: lint, `tsc`, 547 Vitest tests, production build, `check:build-secrets`, existing 90 mocked browser tests. Hosted-path acceptance (`npm run test:e2e:hosted`): 14/14 browser scenarios, 21/21 direct PostgreSQL checks, object-store check. |
| **CI verified** | Pending — see the PR for the check results on the latest commit. |
| **Provider integration verified** | **No.** Real Clerk, real Cloudflare R2 and real Render were not reachable. Clerk is stood in for by a test RSA key + local JWKS (Clerk's own middleware verifies the cookie with a public key; FastAPI verifies against the JWKS); R2 by a mocked S3 service (moto) through the real boto3 client. |
| **Deployed and hosted-smoke-tested** | **No.** Nothing is deployed. |
| **Blocked pending user/provider configuration** | Clerk application, R2 bucket + token, Render Blueprint apply, Neon roles, identity-mapping registration, hosted acceptance. |

## 2. What was built

**Authentication and tenancy**
* `AP_AGENT_AUTH_MODE` = `prototype_headers` (dev/test only) | `clerk_jwt`. `AP_AGENT_ENVIRONMENT=hosted` makes
  startup fail closed (API and worker): prototype auth, missing/malformed Clerk issuer or authorized parties,
  non-https endpoints, local filesystem storage or a leftover artifact root, incomplete S3 settings, operations
  disabled, identical runtime/migration DSNs, a non-PaddleOCR or tenant-scoped worker. Errors name settings, never values.
* FastAPI independently verifies Clerk session tokens (`ap_agent/auth/clerk.py`): RS256 only (none/HS* refused),
  signature, issuer, `exp`/`nbf`/`iat` (required), subject, authorized party (required), optional audience, active
  organization (v1 `org_id` and v2 `o.id` layouts). JWKS cached 1 h, refreshed on unknown key id with a minimum
  interval (rotation), stale keys survive a failed refresh, unreachable + no keys → `503 AUTHENTICATION_UNAVAILABLE`.
  Tokens, payloads, Clerk IDs are never logged or returned (tested).
* Migration `0005_identity_mappings`: `identity_organizations` (one organization ↔ one tenant) and
  `identity_mappings` (provider, external org/user, tenant, `InterfaceRole`, status, timestamps) with uniqueness,
  composite FK (tenant must be the organization's tenant), format/role/status checks, immutability and no-delete
  triggers. The runtime role is granted **read-only** on both tables (`grant_schema_access` revokes writes).
  Tenant and role come only from this table (never headers, bodies, URL parameters or Clerk metadata); the actor
  id recorded for audit is `user-<mapping id>`, not a Clerk ID.
* `scripts/manage_identity_mappings.py`: `register-tenant`, `register`, `update`, `disable`/`enable`,
  `disable-org`/`enable-org`, `inspect`; idempotent; requires `AP_AGENT_POSTGRES_MIGRATION_DSN` (no fallback), never prints it.
* `GET /api/v1/session` returns the mapped role and tenant display name for the UI shell.
* Next.js: `@clerk/nextjs` 7.9.9; `src/proxy.ts` enforces (server side, before any page) sign-in and an active
  organization (pure policy in `lib/auth/route-access.ts`); `/sign-in`, `/sign-up` (invitations), `/organization-required`,
  `/access-denied` (unmapped, inactive, expired, no organization, verification unavailable); account controls
  (organization switcher, user button, explicit sign-out); the server forwards `Authorization: Bearer <session token>`
  (`lib/auth/backend-auth.ts`) from the page, proxy-read, command and upload boundaries and ignores any browser-supplied
  identity. CSRF/same-origin protections are unchanged; hosted mode additionally requires a ≥32-char CSRF secret.

**Cloud storage**
* `S3ArtifactStore` (Cloudflare R2/S3-compatible) behind the existing `ArtifactStore` interface. Keys
  `tenants/<tenant>/jobs/<job>/source/<sha256>` built only from validated hex components (no file name, no traversal,
  tenant checked before any request). Uploads spooled to a private temp file (bounded memory, size-capped, existing
  size/extension/MIME/signature/hash validation unchanged), sent with `ChecksumSHA256`, content type, size and SHA-256
  metadata and read back; re-commit is idempotent. No ACLs, no public URL, no pre-signed URLs, no credentials to the
  browser. The worker downloads into a `0700` temp directory, re-verifies size and SHA-256 against PostgreSQL's
  recorded values, runs the pipeline and removes the directory in `finally` (success and failure). Processing never deletes
  source objects. Local filesystem store retained for dev/tests.

**Hosted deployment**
* `docker/api.Dockerfile`, `docker/web.Dockerfile`, `docker/worker.Dockerfile` (multi-stage, non-root; the worker
  pre-downloads PaddleOCR models at build time and builds the engine at start, exiting 5 with a clear message if it
  cannot; memory guidance ≥ 4 GB).
* `render.yaml`: public web, private API (`pserv`, no public route), one worker (`numInstances: 1`), explicit
  Dockerfiles, `healthCheckPath` on web, secrets `sync: false`, private address via `fromService`, the single
  migration mechanism = the API's `preDeployCommand` (migration DSN; advisory-locked), `autoDeploy: false`.
  Verified by `scripts/verify_render_blueprint.py` (+16 unit tests).
* `/health/live`, `/health/ready` (configuration + database; names/status only), startup diagnostics (variable
  names with `set`/`unset`), graceful worker shutdown (SIGTERM finishes the in-flight job), bounded timeouts
  (DB connect, S3 connect/read, JWKS fetch, uvicorn keep-alive/shutdown, Next→API 30 s).

**Hydration race (M11D known issue) — fixed**
The file input is `disabled` until React has hydrated, and a selection that nevertheless reaches the DOM early is
adopted on hydration. Regression test `OperationsConsoleHydration.test.tsx` fails against the old component
(2 of 3 assertions fail) and passes now; the Playwright retry loops in `real-operations.spec.ts` are removed.

## 3. Tests added

* Backend: Clerk verification (24), hosted configuration (23), auth dependency (22), object-storage contract with a
  fake client incl. failure injection (31), worker hosted configuration (9), boto3 + mocked S3 (3), Blueprint (16),
  identity mapping/CLI/Clerk-authenticated API on real PostgreSQL (16).
* Frontend: server env modes, route-access policy, auth reasons, backend-auth, page identity, hosted boundaries (upload/command),
  account-state components, hydration regression.
* Hosted acceptance (`frontend/scripts/run-hosted-acceptance.mjs` + `hosted-workflow.spec.ts`): unauthenticated →
  sign-in redirect and 401 JSON; tampered/wrong-authorized-party sessions not signed in; FastAPI rejects bad tokens itself
  (tampered, wrong azp, no organization, unmapped, inactive, forged role header ignored); organization required;
  unmapped/inactive states; operator upload accepted and queued (< 20 s, no OCR in the request, no object key/hash in the
  response); worker reads the S3 object → review required → reviewer claim/approve/resume from the recorded stage →
  completed; read-only auditor refused; another organization sees nothing and gets 404; hydration race; sign-out control +
  session end; no secret/token/private host/S3 credential/tenant id in rendered HTML, bundles or browser storage;
  axe (WCAG 2 A/AA + 2.2 AA) desktop and mobile on hosted pages. Afterwards: object store keys are tenant-scoped and
  file-name-free; PostgreSQL checks (21/21: original memory unchanged, one derived version, append-only tables, tenant isolation).

## 4. CI

* `m11a-frontend.yml`: lint/types/unit/build/mocked browser suites (existing) + the new fast M11E backend tests +
  the **single** hosted-mode acceptance run in the real-integration job.
* `m8-postgres-acceptance.yml`: now also runs for this branch; the real-PostgreSQL M11E tests run once, in the
  full regression (ignored by the earlier steps, as M11D's are); real PaddleOCR coverage unchanged.
* `m11e-hosted.yml` (new): Blueprint verification; builds the three production images, checks non-root, scans them
  for secrets, checks hosted fail-closed behaviour of the API and worker images. It does not rerun the slow suites.

## 5. Screenshots (`docs/m11e-screenshots/`)

`01` sign-in redirect, `02` organization required, `03` account states (unmapped, inactive), `04` shell +
operations upload, `05` queued, `06` processing (when observed), `07` review required, `08` review state,
`09` completed, `10` other organization, `11` mobile operations, `12` mobile account state.
**Limitation:** Clerk's own components (sign-in form, organization switcher, user button) are loaded from Clerk's CDN
and cannot render without reaching Clerk, so those widgets are absent from the screenshots; our own pages, states and
the explicit Sign-out button are real. Capture the Clerk-rendered sign-in and shell during hosted acceptance.

## 6. Local test results

* Backend, full suite on local PostgreSQL 16 (not Neon) with Tesseract: **1313 passed, 31 skipped, 0 failed**
  (`tests/unit/test_paddleocr_adapter.py` excluded locally — PaddleOCR is not installed in this sandbox; it runs in CI).
* Frontend: `tsc` clean, `eslint` clean, **548 Vitest tests passed**, production build, `check:build-secrets` clean,
  existing mocked browser suite 90/90.
* Hosted-path acceptance: **14/14 browser scenarios, 21/21 PostgreSQL checks, object-store check passed**
  (OCR provider: real Tesseract via the Phase 3 fallback router, which routes to review by design; real PaddleOCR is covered by CI).

## 7. Deviations and limitations

* **D-1** Branch is the harness-assigned `claude/amazing-euler-546zxj`.
* **D-2** Two existing tests hard-coded "four migrations"; updated to five (a scheduled schema change).
* **D-3** `ArtifactStore` gained `verified_local_copy` / `verify_integrity` (document executor and resume verification use them); behaviour for the local store is unchanged.
* **D-4** Pages moved into `(app)` / `(auth)` route groups (URLs unchanged) so sign-in pages do not render the app shell.
* **D-5** The worker image bundles `tests/fixtures/reference_data` as the MVP reference data (via a `.dockerignore` exception) — sample suppliers/POs; replace with the organization's own data before real use.
* **D-6** `authenticated_at` in Clerk mode is the token `iat` minus 30 s (skew allowance) so commands are never "authenticated after requested".
* **D-7** Redirects to the account-state page from a streaming server render are client-side (HTML shows a loading state first); no application data is rendered.
* Pre-hydration *drag-and-drop* onto the zone is not intercepted (the browser would open the file); the input itself is guarded.
* A killed worker still leaves a job `RUNNING` (no leases — explicitly out of scope).
* R2-specific behaviour (checksum header acceptance, region `auto`) is validated only against a mocked S3 service; confirm in hosted acceptance.
* The migration DSN is present in the API service's environment because it runs the pre-deploy step; the stricter alternative (run migrations from a workstation/CI) is documented in the runbook.
* The Blueprint was written without access to Render's docs from this sandbox; `healthCheckPath` is set on the web service only. Validate with Render's Blueprint check on first apply.

## 8. Remaining user setup (exact, short)

Enter values only in the provider dashboards — never in chat, files, commits or PR comments.
1. **Clerk:** create the application, enable Organizations, disable user-created organizations, restrict sign-up to invitations, create the organization and invite users. Keep: publishable key, secret key, issuer URL (optional: JWT public key).
2. **Cloudflare R2:** private bucket (no public access/domain), API token scoped to that bucket (read+write). Keep: endpoint, bucket, access key ID, secret.
3. **Neon:** a migration/owner DSN (direct) and a least-privilege runtime role DSN (pooled), different roles.
4. **Render:** New → Blueprint → this repo/branch → enter the `sync: false` values per `docs/m11e_deployment_runbook.md` §4 (web: Clerk keys + authorized parties; api: runtime + migration DSN, runtime role name, Clerk issuer + authorized parties, R2 values; worker: runtime DSN, R2 values). Use a ≥ 4 GB plan for the worker.
5. Run the identity CLI (runbook §6) to register the tenant and members, then the acceptance steps (§9).
When this is done, ask to resume: deployment verification, `hosted-smoke.mjs`, and the authenticated acceptance can proceed.

## 9. Deferred (post-MVP enterprise hardening, not M11E blockers)

Multiple workers, leases/heartbeats/crash recovery, dead-letter queues, autoscaling, Redis/Celery, Kubernetes,
SAML/SSO/SCIM, public onboarding, billing, email ingestion, ERP/payment integrations, public API, malware
scanning/CDR, compliance certification, multi-region DR, direct browser-to-R2 uploads.

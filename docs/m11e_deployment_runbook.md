# M11E deployment runbook — hosted MVP (Clerk + Cloudflare R2 + Neon + Render)

This is the operator checklist for the private, authenticated hosted deployment. Everything in it
uses **placeholders**; never paste a real value into the repository, a commit, a PR, a log or chat.
Enter secrets only into the provider dashboards named below.

Architecture: `browser → ap-agent-web (Next.js, public) → ap-agent-api (FastAPI, private) → Neon`,
`ap-agent-worker (single OCR worker) → Neon + R2`. The browser never reaches FastAPI, PostgreSQL or R2.

## 1. Expected recurring services (and cost warning)

| Service | Render type | Instances | Notes |
|---|---|---|---|
| `ap-agent-web` | web (public) | 1 | `starter` plan is enough |
| `ap-agent-api` | private service | 1 | `starter`; also runs the migration pre-deploy step |
| `ap-agent-worker` | background worker | **exactly 1** | PaddleOCR needs ~2–3 GB RAM → the Blueprint selects the `pro` (4 GB) plan |
| Neon PostgreSQL | external | — | existing project; separate runtime and migration roles |
| Cloudflare R2 | external | — | one private bucket |
| Clerk | external | — | one application with Organizations enabled |

> **Cost:** the OCR worker is the dominant recurring cost, billed for as long as it exists (it polls
> continuously). Suspend or delete it when you are not testing. A smaller plan will be OOM-killed on real
> invoices. Worker sizing is the setting to revisit first if cost matters.

## 2. Clerk application and organization setup

1. Create a Clerk application. Enable **Organizations**.
2. Under *Organization settings*: **disable** “Allow new members to create organizations” (organizations are
   provisioned by an administrator through the identity mapping, not self-service).
3. Under *Restrictions*: set sign-up to **restricted / invitation-only** (the app has no public onboarding).
4. Create the organization(s) for the pilot and invite users (they accept via `/sign-up`).
5. Note (do not commit): the **Publishable key** (`pk_…`), the **Secret key** (`sk_…`), the **Frontend API /
   issuer URL** (`https://<your-instance>.clerk.accounts.dev` or your custom domain), and optionally the
   **JWT public key (PEM)** for networkless verification.
6. Clerk proves *who* the user is and *which organization is active*. Roles are **not** read from Clerk:
   the database mapping (section 6) is authoritative, so no paid custom roles are needed.

## 3. Cloudflare R2 (private bucket)

1. Create a bucket (e.g. `<bucket-name>`). Leave **Public access disabled**; do **not** attach a custom
   domain or enable the `r2.dev` URL.
2. Create an **R2 API token** scoped to *Object Read & Write* on **that bucket only** (not account-wide).
3. Note: the S3 endpoint `https://<account-id>.r2.cloudflarestorage.com`, the bucket name, the access key
   ID and the secret access key. Region is `auto`.
4. Objects are stored as `tenants/<tenant>/jobs/<job>/source/<sha256>` — never by file name. The source
   invoices are immutable; workflow processing never deletes them.

## 4. Render Blueprint and environment variables

1. In Render: *New → Blueprint*, select this repository and branch, point it at `render.yaml`.
2. Render creates the three services and prompts for every `sync: false` value. Enter them from the table.
   Values marked **generated** are created by Render. `AP_AGENT_API_HOSTPORT` is wired automatically.

| Variable | web | api | worker | Value / source |
|---|:-:|:-:|:-:|---|
| `AP_AGENT_ENVIRONMENT` | ✔ | ✔ | ✔ | `hosted` (committed) — makes startup fail closed |
| `AP_AGENT_FRONTEND_AUTH_MODE` | ✔ | | | `clerk_jwt` (committed) |
| `AP_AGENT_AUTH_MODE` | | ✔ | | `clerk_jwt` (committed) |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | ✔ | | | Clerk publishable key (public; also a Docker build arg) |
| `CLERK_SECRET_KEY` | ✔ | | | Clerk secret key — **secret, web only** |
| `CLERK_JWT_KEY` | ✔ | | | optional Clerk JWT public key (PEM) |
| `AP_AGENT_CLERK_AUTHORIZED_PARTIES` | ✔ | ✔ | | https origin(s) of the web service, e.g. `https://<web-host>` (comma-separated) |
| `AP_AGENT_CLERK_ISSUER` | | ✔ | | Clerk issuer URL; JWKS is fetched from `<issuer>/.well-known/jwks.json` |
| `AP_AGENT_CLERK_JWKS_URL` | | optional | | override of the JWKS URL |
| `AP_AGENT_FRONTEND_CSRF_SECRET` | ✔ | | | **generated** by Render |
| `AP_AGENT_API_HOSTPORT` | ✔ | | | wired from the API service (private network) |
| `AP_AGENT_BACKEND_TIMEOUT_MS` | ✔ | | | `30000` (committed) |
| `AP_AGENT_FRONTEND_REVIEW_COMMAND_MODE` / `…_OPERATIONS_MODE` | ✔ | | | `commit` / `enabled` (committed) |
| `AP_AGENT_ENABLE_OPERATIONS`, `AP_AGENT_ENABLE_REVIEW_COMMAND_WRITES` | | ✔ | | `true` (committed) |
| `AP_AGENT_POSTGRES_DSN` | | ✔ | ✔ | **runtime** role DSN, Neon *pooled* endpoint, `sslmode=require` |
| `AP_AGENT_POSTGRES_MIGRATION_DSN` | | ✔ | | **migration/admin** DSN, Neon *direct* endpoint — used only by the pre-deploy step |
| `AP_AGENT_POSTGRES_RUNTIME_ROLE` | | ✔ | | name of the runtime role to grant (not a secret) |
| `AP_AGENT_ARTIFACT_STORAGE` | | ✔ | ✔ | `s3` (committed) |
| `AP_AGENT_S3_ENDPOINT_URL` | | ✔ | ✔ | R2 endpoint |
| `AP_AGENT_S3_BUCKET` | | ✔ | ✔ | bucket name |
| `AP_AGENT_S3_ACCESS_KEY_ID` / `…_SECRET_ACCESS_KEY` | | ✔ | ✔ | R2 token credentials — **secret** |
| `AP_AGENT_S3_REGION` | | ✔ | ✔ | `auto` (committed) |
| `AP_AGENT_ENABLE_WORKER_EXECUTION`, `AP_AGENT_WORKER_OCR_PROVIDER` | | | ✔ | `true`, `paddleocr` (committed) |

Credentials are separated by purpose: runtime DSN (api, worker), migration DSN (api pre-deploy only),
Clerk secret (web only), R2 credentials (api, worker). The API and the worker **refuse to start** if the
runtime and migration DSNs are identical, so the schema owner can never silently serve requests.

Hosted startup also fails closed (clear error naming the *setting*, never its value) if: prototype
authentication is selected; Clerk issuer/authorized parties are missing or malformed; the artifact backend
is local or the S3 settings are incomplete or not https; operations are disabled.

## 5. Neon roles and migration

* **Migration/admin role** — owner of the `ap_agent` schema; used only by `scripts/migrate.py`.
* **Runtime role** — `LOGIN`, no `SUPERUSER`, no `BYPASSRLS`; `SELECT/INSERT/UPDATE` on schema tables.
  `scripts/migrate.py` grants it access after migrating when `AP_AGENT_POSTGRES_RUNTIME_ROLE` is set and
  **revokes write access on the identity-mapping tables** from it.

The single migration mechanism is the API service's Blueprint `preDeployCommand`
(`python scripts/migrate.py --require-ssl`): it runs once per deploy before the new API version starts, and
only on that service. The runner also takes a PostgreSQL advisory lock, so two overlapping runs cannot
interleave. To run it by hand (e.g. first deploy, from a trusted workstation):

```bash
AP_AGENT_POSTGRES_MIGRATION_DSN='<migration-dsn>' AP_AGENT_POSTGRES_RUNTIME_ROLE='<runtime-role>' \
  python scripts/migrate.py --require-ssl
```

Stricter alternative: run the migration only from a workstation/CI job and leave
`AP_AGENT_POSTGRES_MIGRATION_DSN` out of Render entirely (then remove `preDeployCommand`).

Migration `0005` (identity mapping) is additive. Earlier tables are untouched.

## 6. Identity mapping (who may sign in, as what)

No user can reach any data until an administrator registers them. Run from a trusted workstation with the
migration/admin DSN (the CLI reads only `AP_AGENT_POSTGRES_MIGRATION_DSN`, never prints it, and never falls
back to the runtime DSN). Clerk IDs come from the Clerk dashboard (`org_…`, `user_…`).

```bash
export AP_AGENT_POSTGRES_MIGRATION_DSN='<migration-dsn>'
# 1. Create the tenant once (a UUID is generated and printed if --tenant-id is omitted).
python scripts/manage_identity_mappings.py register-tenant --tenant-key <tenant-key> --display-name "<Organization Name>"
# 2. Bind the Clerk organization to the tenant and add members (idempotent).
python scripts/manage_identity_mappings.py register --tenant-id <tenant-uuid> --org-id <org_id> --user-id <user_id> --role TENANT_ADMIN
python scripts/manage_identity_mappings.py register --tenant-id <tenant-uuid> --org-id <org_id> --user-id <user_id> --role AP_OPERATOR
# 3. Change a role, disable / re-enable a member, or disable a whole organization.
python scripts/manage_identity_mappings.py update  --org-id <org_id> --user-id <user_id> --role AP_REVIEWER
python scripts/manage_identity_mappings.py disable --org-id <org_id> --user-id <user_id>
python scripts/manage_identity_mappings.py enable  --org-id <org_id> --user-id <user_id>
python scripts/manage_identity_mappings.py disable-org --org-id <org_id>
# 4. Inspect.
python scripts/manage_identity_mappings.py inspect --tenant-id <tenant-uuid>
```

Roles: `TENANT_ADMIN`, `AP_OPERATOR` (uploads), `AP_REVIEWER` (claim/approve/correct/reject/resume),
`READ_ONLY_AUDITOR`. One organization maps to exactly one tenant. Changes take effect on the user's next
request (no cache). Mapping rows are never deleted; disabling is the removal mechanism.

## 7. Deployment order

1. Neon: roles created; Clerk application and R2 bucket/token ready (sections 2, 3, 5).
2. Render: apply the Blueprint, enter the secret values. First deploy runs the migration pre-deploy step.
3. Set `AP_AGENT_CLERK_AUTHORIZED_PARTIES` to the web service's final URL (and any custom domain); redeploy
   web + api if the URL changed. In Clerk, add that URL as an allowed origin if prompted.
4. Register the tenant and mappings (section 6).
5. Verify health (section 8), then run the hosted acceptance (section 9).

## 8. Health verification

* API (private; check from the Render shell or a one-off job): `GET /health/live` → `{"status":"alive"}`;
  `GET /health/ready` → `{"status":"ready","checks":{"configuration":"ok","database":"ok"}}` (503 otherwise;
  names only, never values).
* Web: `GET https://<web-host>/sign-in` → 200. An unauthenticated `GET /dashboard` must redirect to `/sign-in`.
* Worker: logs show `worker: configuration status: …` (names + `set`/`unset`) then
  `worker: started (single worker…)`. If the OCR engine cannot be built it exits with code 5 and
  `the OCR provider could not be initialised` before claiming any job.
* The browser must have **no** route to FastAPI: the API has no public URL (Render private service).

## 9. Hosted acceptance

Automated smoke (no credentials needed; checks the public surface and that nothing leaks):

```bash
HOSTED_BASE_URL=https://<web-host> node frontend/scripts/hosted-smoke.mjs
```

Authenticated end-to-end (manual, ~10 minutes; requires one mapped user per role):

1. Sign in as the `AP_OPERATOR`; the shell shows the organization name and role. Upload a PDF/PNG/JPEG on
   `/operations` → “Accepted and queued” immediately (no OCR in the request).
2. Watch the job go `Queued → Running → Review required` (or `Completed`). Confirm in R2 that exactly one
   object exists under `tenants/<tenant>/jobs/…/source/<sha256>` and that it is **not** publicly readable
   (`curl -I https://<account-id>.r2.cloudflarestorage.com/<bucket>/<key>` → 403/401/404).
3. As the `AP_REVIEWER`: open the case, claim, approve (or correct), request resume; the worker resumes
   from the recorded restart stage and the job ends `Completed`.
4. As the `READ_ONLY_AUDITOR`: can read jobs, cannot upload (message shown).
5. As a user of another organization: sees nothing of the first organization.
6. Sign out → redirected to sign-in; refresh while signed in keeps the session.
7. Account states: an unregistered user sees “This account is not set up yet”; `disable` a member and the
   next request shows “Your access is inactive”.

## 10. Rollback

* **Application:** in Render, redeploy the previous successful deploy of each service (web → api → worker).
  Auto-deploy is off, so nothing moves until you choose it.
* **Database:** migrations are additive and forward-only (checksummed); the previous application version
  keeps working against the newer schema. Do not edit or delete applied migrations. If a migration itself
  is faulty, stop the worker, fix forward with a new migration, and restore from a Neon point-in-time branch
  only as a last resort.
* **Access:** disable a member or organization with the CLI (immediate), or disable the Clerk organization.

## 11. Secret rotation

| Secret | How to rotate |
|---|---|
| Clerk secret key | Create a new key in Clerk, update `CLERK_SECRET_KEY` on web, redeploy, delete the old key |
| Clerk signing keys | Handled by Clerk; FastAPI caches JWKS for 1 h and refreshes on an unknown key id (rate-limited), so rotation needs no redeploy |
| Runtime DB password | `ALTER ROLE <runtime-role> PASSWORD …` (or rerun the role helper), update `AP_AGENT_POSTGRES_DSN` on api and worker, redeploy |
| Migration DSN | rotate the owner password in Neon, update the api service variable (or your workstation) |
| R2 token | create a new bucket-scoped token, update the four S3 variables on api and worker, redeploy, revoke the old token |
| CSRF secret | change `AP_AGENT_FRONTEND_CSRF_SECRET` (open forms need a reload) |

## 12. Logs and troubleshooting

Logs never contain bearer tokens, cookies, JWT payloads, Clerk IDs, DSNs or storage credentials; startup
logs list variable **names** with `set`/`unset` only.

| Symptom | Likely cause |
|---|---|
| API exits at start with `HOSTED_REQUIRES_…` / `CLERK_…` / `S3_…` codes | the named setting is missing or unsafe (see section 4) |
| `RUNTIME_DSN_EQUALS_MIGRATION_DSN` | the runtime DSN was set to the owner DSN — use the least-privilege role |
| Every page → “Sign-in could not be verified right now” (`AUTHENTICATION_UNAVAILABLE`) | API cannot reach Clerk's JWKS (check `AP_AGENT_CLERK_ISSUER`/egress) |
| `TOKEN_AUTHORIZED_PARTY_INVALID` | `AP_AGENT_CLERK_AUTHORIZED_PARTIES` does not include the web origin |
| “This account is not set up yet” | no mapping — register it (section 6) |
| Upload → “Invoice storage is temporarily unavailable” | R2 endpoint/credentials/permissions; `STORAGE_UNAVAILABLE` in the API log |
| Job stays `QUEUED` | worker not running / crashed at start (exit code 5 = OCR engine, 4 = configuration) |
| Job stays `RUNNING` after a worker restart | known M11D/M11E limitation (no leases): re-submit the invoice; see the report |
| Worker restarts repeatedly | out of memory — use a ≥ 4 GB plan |

## 13. Explicitly out of scope (post-MVP enterprise hardening)

Multiple workers, leases/heartbeats/crash recovery, dead-letter queues, autoscaling, Redis/Celery,
Kubernetes, SAML/SSO/SCIM, public onboarding, billing, email ingestion, ERP/payment integrations, public API,
malware scanning/CDR, compliance certification, multi-region DR, direct browser-to-R2 uploads.

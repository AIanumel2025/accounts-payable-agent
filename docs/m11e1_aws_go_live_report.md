# M11E.1 — Cost-optimised AWS go-live: report

**Status: State B — infrastructure, application changes and tests are complete; the stack is NOT deployed.**
No AWS stack exists, no migration ran, no hosted acceptance was performed and there is **no public URL yet**. The only
blockers are the operator's AWS session and the user-held Clerk/Neon secrets. The ordered procedure that takes you
from here to a working URL is `docs/m11e1_aws_deployment_runbook.md` (§3–§8; the condensed version is in the PR
description). The PR is open and unmerged.

Starting commit `dc53dd1` (M11E merged). Branch: `claude/great-bardeen-w8wwlz` — the branch assigned by the
execution harness, **not** `claude/m11e1-aws-go-live` (deviation D-1). The working tree was clean at the start.

## 1. What could and could not be verified (read first)

| Level | Status |
|---|---|
| **Verified locally** | New and existing backend tests (real PostgreSQL 16 for the database-backed ones, incl. the real-PaddleOCR worker tests once PaddleOCR was installed); frontend type-check, lint, 596 unit/component tests, production build (plain and standalone), build-secret scan; the **existing** hosted acceptance (14/14 browser scenarios, 21/21 database checks, object-store check) proving the Render/R2 path is unchanged; `cfn-lint` (3 templates) and `sam validate --lint`; the policy checks of `scripts/verify_aws_templates.py`; the standalone Next.js server started with the Lambda entry point; a real-PaddleOCR benchmark of the worker path (§6). |
| **Verified in CI (to confirm on the PR)** | The four Lambda images are built, size-checked (< 10 GB), smoke-tested and scanned; the worker image serves a real OCR prediction under a **read-only root filesystem with no network**; the same benchmark runs inside the worker image under a 4 vCPU / 8 GB ceiling with the model/font hosts blocked. See §9. |
| **Not verified (needs AWS)** | Everything that touches a real AWS service or provider: CloudFormation creating the stack, Lambda Web Adapter behaviour with these images, SigV4 acceptance by a real `AWS_IAM` Function URL, S3 acceptance of the presigned POST policy, SQS/Lambda event-source behaviour, real Clerk sign-in, real Neon, cold-start times. The Lambda images could not be built in the authoring sandbox (its build containers have no network and `public.ecr.aws` is blocked), which is why CI builds them. |
| **Deployed / hosted acceptance** | **No.** See §10 for the exact pending checks. |

The authoring session found ambient AWS credentials in its environment. A read-only identity check with them was
blocked by the session's auto-mode credential policy; it was not retried or worked around, and no AWS call was made at
any point. Nothing in this PR or its history contains a credential, account id, DSN, Clerk secret or token.

## 2. Final architecture

```
browser ── HTTPS ──► web Lambda  (Next.js 16 standalone + Lambda Web Adapter, public Function URL, Clerk)
   │                    │  SigV4 (execution role), Clerk token in X-AP-Agent-Clerk-Authorization
   │                    ▼
   │                api Lambda   (FastAPI + Lambda Web Adapter, Function URL AuthType AWS_IAM)
   │                    │  ├── Neon PostgreSQL (runtime role, DSN read from SSM at cold start)
   │                    │  ├── S3 (presign POST, validate staged object, commit immutable object)
   │                    │  └── SQS FIFO  ── send {job_id, tenant_id, dispatch_generation}
   └── presigned POST ──► private S3 bucket          │
                                                     ▼
                              worker Lambda (SQS trigger, batch 1, reserved concurrency 1, 900 s)
                                 └─ per job: claim (QUEUED→RUNNING) → fresh child process → real PaddleOCR → Neon / S3 (read-only)
migration Lambda (no URL, no trigger; invoked by an operator) ── migration/owner DSN ── Neon
DLQ (FIFO) ◄── redrive after 3 receives
```

Resources (`deploy/aws/template.yaml`, SAM): 4 container-image Lambda functions (web, api, worker, migrate), 2 Function
URLs (public / IAM), SQS FIFO queue + FIFO DLQ + redrive, private S3 bucket (+ policy), 4 IAM roles (one per function),
4 log groups (14 days), SNS topic + 6 alarms, outputs. `deploy/aws/ecr.yaml`: 4 ECR repositories with lifecycle rules
(keep 3 images, expire untagged after 1 day). `deploy/aws/fargate-fallback.yaml`: an **isolated, optional** on-demand
ECS Fargate worker (EventBridge Pipes → one task per message; no service, NAT or load balancer), not deployed by default.

Required outputs: `FrontendUrl`, `ApiUrl`, `UploadBucketName`, `QueueUrl`, `WorkerFunctionArn`, `MigrationFunctionArn`
(plus `DeadLetterQueueUrl`, `AlarmTopicArn`). Tags `Project=accounts-payable-agent`, `Environment=production`,
`ManagedBy=cloudformation` on every taggable resource and function.

### Application changes (all additive; every new setting defaults to the existing behaviour)

* **Platform auth mode** `AP_AGENT_PLATFORM_AUTH_MODE=aws_sigv4` (FastAPI) / `AP_AGENT_FRONTEND_PLATFORM_AUTH_MODE` (Next.js).
* **Native S3 storage** `AP_AGENT_ARTIFACT_STORAGE=aws_s3` (execution-role credentials, same tenant-scoped content-addressed
  keys). The Cloudflare R2 implementation is untouched.
* **Staged direct uploads** (`POST /api/v1/operations/upload-intents`, `POST …/{id}/finalize`; browser mode
  `AP_AGENT_FRONTEND_UPLOAD_MODE=s3_direct`). The multipart route stays for Render.
* **SQS dispatch** (`AP_AGENT_QUEUE_BACKEND=sqs`) after the job commit — for new uploads and for review-resume jobs.
* **Queue-driven worker** (`ap_agent.worker.lambda_handler`), one-job-per-process runner (`ap_agent.worker.run_job`),
  Fargate one-shot entry (`ap_agent.worker.dispatch_task`).
* **SSM secret loading** at cold start (Python `ap_agent.aws.secrets`, Node `frontend/lambda/bootstrap.mjs`), the
  **migration/admin task** (`ap_agent.aws.migration_handler`), **PaddleOCR asset preparation** for a read-only image
  (`ap_agent.aws.paddle_cache`).
* New repository methods `peek_job` / `claim_job` (the polling `claim_next_job` is unchanged).
* No database migration was added: schema, RLS, append-only triggers and the effective-memory views are untouched.

## 3. Security boundaries

| Boundary | Enforcement | Evidence |
|---|---|---|
| Browser ⇄ server | The browser talks only to the Next.js origin (and, for the one upload step, the presigned S3 URL FastAPI issued). It never receives the API URL, database or AWS credentials, or server secrets. Same-origin + per-page CSRF on every POST; the bundle scan extends to the AWS wiring (`lambda-url`, `AP_AGENT_SSM_PARAMETERS`, the dedicated header name, `AWS_SECRET_ACCESS_KEY`, `ASIA…`, SQS URLs). | `m11e1-upload-intent-boundary.test.ts`, `check:build-secrets` (clean on the standalone and the plain build), hosted acceptance scenario 13 |
| Header forgery | `x-ap-agent-clerk-authorization` (and the prototype identity headers) are **stripped from every inbound request** by `src/proxy.ts`; `upstreamHeaders` discards any pre-existing copy and rebuilds the header only from the Clerk-derived `Authorization` produced by `getBackendAuthHeaders`. FastAPI reads the header **only** when `AP_AGENT_PLATFORM_AUTH_MODE=aws_sigv4`; in bearer mode it is ignored, and in sigv4 mode `Authorization` (the SigV4 signature) is never parsed as a token. | `m11e1-aws-boundary.test.ts` (header forgery, bearer unchanged), `test_m11e1_platform_auth.py` (alt header ignored in bearer mode; sigv4 `Authorization` never a token; forged tenant/role headers ignored; tampered/unmapped tokens refused) |
| API reachability | `AuthType AWS_IAM`; `lambda:InvokeFunctionUrl` is granted only to the web function's role (conditioned on the AWS_IAM auth type); no other role has it. | template + `test_m11e1_aws_templates.py` |
| Identity | Unchanged: FastAPI verifies the Clerk token itself (RS256, issuer, azp, expiry, organization); tenant and role come only from the database mapping; unmapped/inactive users are denied. The hosted configuration validator refuses `aws_sigv4` without Clerk or outside `hosted`. | existing M11E suites + new platform tests |
| Uploads | Presigned POST pins bucket, exact key (`staging/<tenant>/<intent>` — no file name), content type, **exact** content length and metadata (tenant, actor, intent, SHA-256, size, encoded name, expiry); 5-minute policy, 30-minute finalize window, 1-day lifecycle expiry. **Finalize** re-derives everything from the stored object: tenant-scoped key (another tenant's intent does not exist), owner actor, expiry, declared size/type, byte-for-byte SHA-256 on a private copy (no TOCTOU), file signature/structure, then the unchanged `submit_upload` commit to the immutable content-addressed key. One intent → one job (idempotency key derived from the intent); the staging object is deleted afterwards. The auditor and reviewer roles are refused before any signing. | `test_m11e1_direct_upload.py` (21), `test_m11e1_upload_routes.py` (7) |
| Queue | Messages carry only job id, tenant id, generation. The worker re-reads the job, requires the tenant to match, claims it **only while `QUEUED`**; duplicates, replays and finished jobs are no-ops; poison messages are redriven to the DLQ. Only the API role can send; only the worker role can receive. | `test_m11e1_queue_worker.py` (34) + 5 real-PostgreSQL tests |
| Secrets | SSM SecureStrings read at runtime with per-function `ssm:GetParameter` on exact parameter ARNs; Lambda environment variables hold only parameter *names*; no secret in templates, parameter files, layers, build args or bundles. The migration DSN is readable by the migration role only. | `verify_aws_templates.py`, `test_m11e1_aws_templates.py` (secrets, privilege rules), secret scans |
| Storage | Block Public Access (all four), owner-enforced ownership (no ACLs), SSE-S3, TLS-only bucket policy, CORS limited to `POST` from the single frontend origin (absent until pass 2), worker read-only on `tenants/*`, web/migrate no S3 access. | template + tests |
| Database | Separate migration and runtime roles; the runtime DSN is the only one the API/worker can read; the API/worker still refuse to start if both DSNs are equal; `AP_AGENT_TEST_POSTGRES_DSN` is never used. | existing + new tests |

## 4. Cost controls

None of: NAT Gateway, load balancer, always-on EC2/ECS, RDS, provisioned concurrency, OpenSearch, ElastiCache, Route 53,
custom domain, VPC, API Gateway, CloudFront, DynamoDB — enforced by `verify_aws_templates.py` (+ mutation tests). Used:
scale-to-zero Lambda, reserved-concurrency bounds (worker 1; web and API 10 each, optional), 14-day logs, S3 lifecycle
rules, ECR lifecycle rules, API connection pool disabled (no idle Neon connections), 6 alarms, one SNS topic.

Estimated low-volume cost: **≈ USD 1–3 / month** for AWS (details and the components most likely to consume the USD 20
budget — the OCR worker at volume, ECR storage of the multi-GB worker image, alarms/logs — in the runbook §15). Neon and
Clerk are billed by their own plans. The existing USD 20 budget and alerts are not touched; no budget was created.

## 5. Tests and exact results

Run on the final tree. Environment: Python 3.11.15, Node 22, local PostgreSQL 16 (not Neon; the M8 test database
`ap_agent_m8_test` on a throw-away local cluster), PaddleOCR installed.

| Suite | Result |
|---|---|
| Backend, full (`pytest -m "not requires_paddle" --ignore=tests/unit/test_paddleocr_adapter.py`) | **1441 passed, 31 deselected, 0 failed** (225 s; the M11E baseline was 1313 passed, 31 skipped). The 31 deselected are the real-PaddleOCR tests; a local attempt with PaddleOCR installed ran for over 80 CPU-minutes and was stopped, so they are left to CI (`m8-postgres-acceptance.yml`) — they are *not* claimed as passed here. |
| New backend unit tests (`tests/unit/test_m11e1_*.py`) | 113 |
| New real-PostgreSQL tests (`tests/api/test_m11e1_queue_postgres.py`) | 5 |
| Frontend `tsc --noEmit`, `eslint .` | clean |
| Frontend Vitest | **596 passed** (49 files; 548 before this milestone; 48 new) |
| Frontend production build, plain and `AP_AGENT_NEXT_OUTPUT=standalone`; `check:build-secrets` | OK; 27 static files scanned, no finding |
| Standalone server via `lambda/entrypoint.mjs` | starts; `/icon.svg` 200; anonymous `/dashboard` → 307 sign-in; forged-header POST → 401 |
| **Existing hosted acceptance** (`npm run test:e2e:hosted`, Render/R2-style stack, mocked S3, real PostgreSQL, worker) | **14/14 browser scenarios, 21/21 database checks, object-store check PASSED** (OCR provider: Tesseract fallback router, as in M11E) |
| `cfn-lint` 1.57.1 on `template.yaml`, `ecr.yaml`, `fargate-fallback.yaml` | clean |
| `sam validate --lint` (placeholder credentials; no AWS call) | valid SAM template |
| `scripts/verify_aws_templates.py` + 19 mutation tests | OK |
| OpenAPI contract (`npm run api:check`) | regenerated; in sync (adds the two routes and the documented header) |

Pre-existing vs new: no failure was observed in any pre-existing suite. Two failures seen while iterating were mine and
were fixed before commit (a duplicate key in the upload error table; the bearer-mode transport must pass `init` through
unchanged). The hosted acceptance needed the sandbox's `HTTPS_PROXY` unset and the pre-installed Chromium path — local
environment details, not code issues.

## 6. PaddleOCR benchmark (worker path, real PaddleOCR)

Method (`scripts/benchmark_worker.py`): for each of the four controlled fixture invoices, an upload job is created in a
real PostgreSQL database, claimed, and executed in a **fresh child process** (`ap_agent.worker.run_job` — the process model
the Lambda handler uses), including PaddleOCR engine initialisation each time. Reported: wall-clock per job and peak
resident memory per job process.

**Authoring-sandbox run (indicative, not a Lambda measurement):** 3 pinned CPU cores, `OMP_NUM_THREADS=3`, no memory cgroup,
models cached locally, local PostgreSQL:

| Document | Seconds | Outcome | Job-process peak RSS |
|---|---:|---|---:|
| `08181_flat_document.png` (clean flat invoice) | 62.2 | SUCCEEDED | 3,613 MB |
| `08181_warped_document_perspective_shadow.jpg` | 82.0 | REVIEW_REQUIRED | 4,643 MB |
| `Template1_Instance90.jpg` | 29.8 | REVIEW_REQUIRED | 1,096 MB |
| `invoice_Aaron Bergman_36258.pdf` (15 KB) | 139.8 | REVIEW_REQUIRED | **6,794 MB** |

Outcomes are those of the validated pipeline (the same statuses the golden results record); nothing was substituted or
weakened — the provider is PaddleOCR throughout.

**Reading:** time is comfortably inside the limit (slowest 140 s ≈ 2.3 minutes against the 12-minute target / 15-minute
Lambda maximum). **Memory is the binding constraint**: the peak reached 6.8 GB on a *15 KB* PDF (the rendered page plus
the document-unwarping model), and it is process-wide — an earlier in-process run of the same four jobs climbed from
3.7 GB to 6.9 GB because PaddleOCR does not return memory between documents. Two consequences were built in:
(1) **every job runs in a fresh child process** (memory is released after each job; an OOM kill, crash or timeout of the
child is recorded on the job as `WORKER_PROCESS_FAILED` / `WORKER_TIMEOUT` instead of leaving it `RUNNING`);
(2) the worker memory default is **8192 MB** (~4.6 vCPU), not the suggested 4096 MB (deviation D-3). At 4096 MB the PDF
and the warped image would be out-of-memory-killed.

Image size: the worker image carries PaddlePaddle 3.3.1, PaddleOCR 3.7.0, PyMuPDF, OpenCV, Tesseract (the existing
Phase 3 fallback), 171 MB of models and 21 MB of fonts; CI asserts the uncompressed size is under Lambda's 10 GB limit
and reports it in the job summary. **Models and fonts are baked into the image**: the first local run revealed that
PaddleX downloads two fonts on first use, so the build now fetches them too and a read-only-root / no-network smoke test
and a blocked-host benchmark enforce "no run-time download".

**Verdict:** Lambda is viable *for the controlled invoices at 8192 MB*. It is **not proven for the largest allowed input**
(a 10 MB, many-page scan could need more than the 10,240 MB Lambda maximum). The Fargate fallback is therefore kept ready
and isolated; the alarms (`WorkerDurationAlarm` > 12 min, `WorkerErrorsAlarm`) and the `WORKER_PROCESS_FAILED` job error
are the triggers to use it. The authoritative Lambda-like numbers come from the CI benchmark (§9); re-confirm them on the
PR before relying on this verdict.

## 7. Deployed resource summary and hosted URL

Nothing is deployed: no stack, no resources, **no hosted URL**. After the runbook is executed the resource list is the
set in §2 and the URL is the `FrontendUrl` output.

## 8. Acceptance evidence against the 18 required hosted checks

| # | Check | Status |
|---|---|---|
| 1 | Public URL loads | **pending** (needs deploy) — `smoke.sh` automates the unauthenticated part |
| 2 | Anonymous → Clerk sign-in | locally proven for the standalone server (307 → `/sign-in`); **pending** against the real URL/Clerk |
| 3 | Organization member sees the interface | proven locally with Clerk-style tokens (hosted acceptance); **pending** real Clerk |
| 4 | Auditor can view, cannot upload | proven locally (hosted acceptance; `test_m11e1_upload_routes.py` 403 on intent and finalize); **pending** hosted |
| 5 | Forged tenant/role/alternate headers fail | proven by unit/route/boundary tests and `smoke.sh` design; **pending** hosted |
| 6 | Cross-organization access fails | proven locally (hosted acceptance scenario 10; finalize returns 404 for another tenant); **pending** hosted |
| 7–8 | Valid invoice up to 10 MB through the staged S3 flow; request returns without OCR | proven at API/boundary level with fakes (no OCR in the request, 10 MB accepted, one job, one SQS message); **pending** real S3 POST policy acceptance |
| 9–10 | SQS dispatches; worker runs real PaddleOCR | dispatch message, claim and isolated execution proven on real PostgreSQL; real PaddleOCR proven by the benchmark; **pending** the AWS wiring |
| 11–14 | Completion/review, claim/approve/correct, resume from the recorded stage, original memory unchanged, derived version appended | proven locally by the existing hosted acceptance (21/21 DB checks) on the unchanged pipeline; **pending** hosted |
| 15 | S3 objects private | template enforces; `smoke.sh` verifies; **pending** |
| 16 | Duplicate delivery does not duplicate processing | proven on real PostgreSQL (3 deliveries → 1 claim, 1 execution, 1 `JOB_CLAIMED`); **pending** hosted replay |
| 17 | No secret in HTML/JS/storage/source maps/logs/assets | bundle scan clean; hosted acceptance scenario 13 passed; logs name settings only; **pending** hosted |
| 18 | Render/R2 mode and existing suites intact | **verified**: existing hosted acceptance 14/14 + 21/21; full backend and frontend suites green |

## 9. CI

* `m11e1-aws.yml` (new): `templates` (cfn-lint, `sam validate --lint`, policy checks, no-secret grep, script syntax);
  `images` ×4 (build, uncompressed-size < 10 GB, secret scan, per-image smoke — API starts/fails closed, web standalone
  redirects/refuses forged header/clean bundle, worker offline read-only OCR prediction, migration handler);
  `worker-benchmark` (real PaddleOCR, 4 vCPU / 8 GB, read-only root, model/font hosts blocked, ≤ 720 s per document,
  result in the job summary and as an artifact).
* `m11a-frontend.yml`: runs the new M11E.1 unit tests; `m8-postgres-acceptance.yml`: the full regression including the
  real-PostgreSQL M11E.1 tests and real PaddleOCR now also runs for this branch.
* CI result at the time of writing: see the PR (checks were running when this report was written).

## 10. Remaining hosted checks and the single hand-off

All 18 checks above marked *pending* remain. Everything needed is in `docs/m11e1_aws_deployment_runbook.md`; in short:
(1) AWS CloudShell/SSO session + Lambda concurrency quota ≥ 11; (2) `create-secrets.sh` (Neon runtime DSN, Neon migration
DSN, Clerk secret key — hidden prompts); (3) `build-and-push.sh`; (4) `deploy.sh pass1`; (5) `invoke-migration.sh migrate`;
(6) `invoke-migration.sh identity register-tenant / register …`; (7) `deploy.sh pass2`; (8) `smoke.sh`, then the
authenticated checks with an invited Clerk test user and a real invoice; (9) read the benchmark from the PR checks and
adjust `WORKER_MEMORY_MB` if it differs.

## 11. Deviations

* **D-1** Branch is the harness-assigned `claude/great-bardeen-w8wwlz`, not `claude/m11e1-aws-go-live`.
* **D-2** Not deployed (State B): AWS access was not available to the session (see §1).
* **D-3** Worker memory default **8192 MB** instead of the suggested initial 4096 MB, because the benchmark measured a 6.8 GB
  per-job peak. The parameter is `WorkerMemoryMb` (min 4096, max 10240).
* **D-4** Upload intents are **stateless** (no table, no migration): ownership, expiry and size/digest travel as
  policy-enforced S3 object metadata and are checked at finalize; single use is enforced by the deterministic idempotency
  key (one job per intent) plus deleting the staging object. A database-backed intent table would add a migration and RLS
  surface for no extra guarantee here.
* **D-5** "Dispatch generation" is `1` for the first dispatch and is part of the FIFO de-duplication id; there is no stored
  counter. Re-dispatching a job still `QUEUED` re-sends the same generation (de-duplicated by SQS for 5 minutes, otherwise
  a duplicate that the idempotent claim ignores).
* **D-6** A job interrupted by a dead invocation is **failed, not re-run** (`WORKER_INTERRUPTED`); with the isolated child
  process the common causes (OOM, timeout) are recorded immediately as `WORKER_PROCESS_FAILED` / `WORKER_TIMEOUT`.
* **D-7** Web/API reserved concurrency is a parameter (`-1` = unreserved) because new accounts commonly have a Lambda
  concurrency quota of 10, which forbids any reservation; the worker's reservation of 1 needs a quota ≥ 11. FIFO ordering
  with one message group already serialises the worker.
* **D-8** The Lambda readiness check uses `/icon.svg` (a static asset) rather than `/sign-in`, which depends on Clerk.
* **D-9** The presigned POST does not use S3's optional `x-amz-checksum-*` form fields; the SHA-256 is verified at finalize
  on a private copy of the stored bytes (the authoritative check).
* **D-10** `s3:ListBucket` is granted to the API and worker roles (without it S3 answers 403 instead of 404 for a missing
  key, which the store must tell apart).
* **D-11** The OpenAPI contract changed (new routes; every authenticated route documents the optional dedicated header).
* **D-12** Pass 1 leaves the API deliberately closed (no Clerk authorized party) and S3 CORS absent until pass 2.

## 12. Unresolved risks

1. **Unproven on AWS** (see §1). Highest-impact unknowns: SigV4 acceptance by a real Function URL (our signer is tested
   against the AWS reference algorithm, not the service); S3's acceptance of the exact presigned-POST policy (tested
   structurally and with fakes, not against S3); Lambda Web Adapter version `0.9.1` with these images; the current
   public-Function-URL permission requirement (`lambda:InvokeFunction` with `InvokedViaFunctionUrl`) is included
   explicitly — if CloudFormation rejects that property in your account, drop it or use the console's equivalent.
2. **Worker memory for the largest inputs** (§6) — possibly beyond Lambda's maximum; Fargate fallback is ready but not
   benchmarked or deployed.
3. **Cold starts**: web/API cold starts add SSM reads (tens of ms) plus runtime start-up; the worker pays PaddleOCR
   initialisation (~3–12 s) per job by design; Neon scale-to-zero adds its own wake-up latency.
4. **Clerk behind the adapter**: redirects rely on `x-forwarded-proto: https` from the Function URL; verify the sign-in
   redirect lands on `https://`.
5. **Quota**: Lambda concurrency quota below 11 blocks the deployment until raised (preflight reports it).
6. **Fallback path limits**: a crashed Fargate task leaves its job `RUNNING` (Pipes already deleted the message).
7. **Single shared SNS email subscription** must be confirmed by the recipient.
8. The MVP reference data (`tests/fixtures/reference_data`) is bundled into the worker image (D-5 of M11E) — replace it
   with real master data before real use.

## 13. Rollback

Per-layer, from the runbook §12: redeploy the previous image tags or commit (`./build-and-push.sh` +
`./deploy.sh pass2`); CloudFormation rolls back failed updates itself; migrations are additive and not reversed (use a
Neon restore if ever required); emergency stop = disable the worker's event-source mapping and/or set the API reserved
concurrency to 0 (messages wait in SQS for 4 days); complete removal = `teardown.sh` then `verify-teardown.sh`. The
Render/R2 deployment is untouched and remains a fallback.

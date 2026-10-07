# M11E.1 / M11E.2 runbook — AWS deployment (Lambda + SQS + on-demand Fargate OCR + S3, Neon, Clerk)

Operator procedure for the cost-optimised AWS deployment (region **eu-west-2**). It deploys the unchanged
application as scale-to-zero Lambda functions plus **one on-demand ECS Fargate task per invoice for the heavy OCR**
(M11E.2: this AWS account caps Lambda memory at 3,008 MB, so the 8 GB PaddleOCR worker cannot be a Lambda); the Render/R2 deployment in `docs/m11e_deployment_runbook.md`
keeps working side by side. Everything here uses **placeholders**. Never paste a secret into a file, a commit, a PR,
a log or chat: secrets are typed only at the hidden prompts of `create-secrets.sh` (or the provider dashboards).

```
browser ──► frontend Lambda (public Function URL, Clerk sign-in) ──SigV4──► API Lambda (AWS_IAM Function URL)
   │                                                                          │  ├─► Neon PostgreSQL (runtime role)
   └─ presigned POST ─► private S3 bucket  ◄── API: validates + finalizes ────┘  └─► SQS FIFO (1 group, batch 1)
                                                                                        │
                       dispatcher Lambda (256 MB, no OCR, no secrets) ◄─────────────────┘
                          │  ecs:RunTask (one task) → waits for STOPPED → exit code 0 ⇒ message deleted; else raise (retry → DLQ)
                          ▼
                 ECS Fargate task, 4 vCPU / 8 GB, public subnet, NO inbound, no NAT  (PaddleOCR; Neon + S3 read-only; DSN from SSM)
migration Lambda (no URL; invoked by an operator) ─► Neon (migration/owner role)
```

> **Why the worker is not a Lambda any more.** The first real deployment failed at the worker function with
> `'MemorySize' ... must be less than or equal to 3008`: this account limits Lambda memory to 3,008 MB. The benchmark
> measured per-job peak resident sets of up to ~6.6–6.8 GB, so a 3,008 MB worker would be killed by the kernel; lowering
> the memory is **not** an option. The OCR therefore runs in a Fargate task with the benchmarked 4 vCPU / 8 GB, started only
> when a job exists. Lambda keeps only small functions (all ≤ 1,024 MB). Details: `docs/m11e2_fargate_ocr_correction.md`.

## 1. Prerequisites (one-time, before the first deployment)

| Need | How |
|---|---|
| AWS credentials | **AWS CloudShell** (recommended) or a workstation signed in with IAM Identity Center (`aws sso login`). Never create long-lived access keys. The identity needs permission to manage CloudFormation, Lambda, **ECS, EC2 (VPC/subnets/security groups)**, IAM roles, S3, SQS, SSM, ECR, CloudWatch and SNS in the account. |
| Budget | The USD 20/month budget and its 50/80/100 % alerts already exist. **Do not create another.** |
| Tools | `aws` v2, `docker`, `sam` (AWS SAM CLI), `jq`, `git`. CloudShell has `aws`, `docker` and `git`; install SAM and jq per section 3. |
| Lambda memory cap | This account allows at most **3,008 MB per Lambda**. Every Lambda in the template is ≤ 1,024 MB; `preflight.sh` fails if one is not. **Do not lower the OCR worker's memory** — it needs ≥ 8 GB and runs on Fargate. |
| Fargate vCPU quota | **AWS Fargate On-Demand vCPU quota ≥ 4** (Service Quotas → AWS Fargate; `preflight.sh` reads it). New accounts sometimes start lower: request an increase before deploying. |
| VPC quota | The stack creates one VPC (default limit 5 per region), an internet gateway and two public subnets — no NAT gateway, no VPC endpoints. |
| Lambda concurrency quota | **No increase needed.** Nothing is reserved by default: the single worker comes from the SQS FIFO design (one message group `ap-agent-jobs`, event-source batch size 1). Lambda requires an account to keep 100 units unreserved, so reserved concurrency would need a quota of at least 101 and is deliberately not used for the worker. The optional `WEB_API_RESERVED_CONCURRENCY=N` cap (default `-1` = off) needs a quota of at least 100 + 2N. |
| Clerk | Application with **Organizations** on, user-created organizations **off**, sign-up **invitation-only**, the organization created and the pilot users invited (see `docs/m11e_deployment_runbook.md` §2). Note the **publishable key** (`pk_…`, public), the **secret key** (`sk_…`, secret) and the **issuer URL** (`https://<instance>.clerk.accounts.dev`). |
| Neon | Existing project with two roles: a **migration/owner** role (direct endpoint DSN) and a **least-privilege runtime** role (`LOGIN`, no `SUPERUSER`, no `BYPASSRLS`; pooled endpoint DSN). Both with `sslmode=require`. They must be different roles. |
| Disk for images | The worker image is several GB (it runs on Fargate with 30 GiB of task storage). CloudShell's Docker may run out of space: if `docker build` fails with "no space left", build on any machine with Docker and an AWS session instead (`build-and-push.sh` works unchanged anywhere). |

Never use `AP_AGENT_TEST_POSTGRES_DSN` here: it exists only for the automated test databases.

## 2. Get the code

```bash
git clone https://github.com/aianumel2025/accounts-payable-agent.git && cd accounts-payable-agent
git checkout claude/great-bardeen-w8wwlz      # the M11E.1 branch (merge status: PR open, unmerged)
cd deploy/aws/scripts
```

## 3. Tooling check (CloudShell)

```bash
pip3 install --user aws-sam-cli && export PATH="$HOME/.local/bin:$PATH"     # if `sam` is missing
sudo yum install -y jq 2>/dev/null || true                                  # if `jq` is missing
export AWS_REGION=eu-west-2
./preflight.sh        # read-only; masks the account id; exits non-zero if any BLOCK line appears
```

`preflight.sh` separates **blocking** from **informational** results and prints no secret:

| Check | Kind |
|---|---|
| Every Lambda in the template ≤ 3,008 MB (`scripts/verify_aws_templates.py --lambda-memory-limit`) | blocking |
| Fargate On-Demand vCPU quota ≥ 4 | blocking if readable and below 4; informational if it cannot be read |
| Existing application stack in `ROLLBACK_COMPLETE` / `CREATE_FAILED` / `*_IN_PROGRESS` | blocking (the message gives the stack-only delete command) |
| The four images in `deploy/aws/.local/images.env` exist in ECR (by tag or digest) | blocking |
| The four SSM SecureString parameters exist (existence only, never decrypted) | blocking |
| Lambda concurrency quota, VPC count, region note | informational |

## 4. Secrets (SSM Parameter Store, SecureString)

```bash
./create-secrets.sh
```

It asks, with hidden input, for: the Neon **runtime** DSN, the Neon **migration** DSN and the Clerk **secret** key. It
generates the frontend CSRF secret itself. Values are stored as SecureStrings under `/ap-agent/production/`:

| Parameter | Read by |
|---|---|
| `postgres-runtime-dsn` | API function, **OCR Fargate task role** (read by the OCR process at start-up) |
| `postgres-migration-dsn` | **migration function only** |
| `clerk-secret-key` | frontend function only |
| `frontend-csrf-secret` | frontend function only |

Nothing secret is placed in CloudFormation, a parameter file, an image layer, a Docker build argument, a Lambda
environment variable (they hold only the *names* of the parameters) or the browser bundle. To rotate: re-run
`create-secrets.sh` and redeploy (section 8) — functions read secrets at cold start.

## 5. First deployment

```bash
export CLERK_PUBLISHABLE_KEY='pk_...'                    # public by design
export CLERK_ISSUER='https://<instance>.clerk.accounts.dev'
export ALARM_EMAIL='you@example.com'                     # optional; SNS asks you to confirm
# export OCR_TASK_MAX_SECONDS=720                        # in-container hard deadline of an OCR task (default 720)
# export WEB_API_RESERVED_CONCURRENCY=10                 # OPTIONAL cap; default -1 (off); needs a quota of at least 100 + 2N

./build-and-push.sh      # creates the ECR stack (lifecycle: keep 3 images), builds + pushes 4 images, records them BY DIGEST
./preflight.sh           # must end with "Preflight passed"
./deploy.sh pass1        # creates the application stack; FrontendOrigin is not passed at all; prints the public FrontendUrl
```

`deploy.sh` passes an explicit `--image-repositories` mapping for every SAM image function (`ApiFunction`, `WebFunction`,
`MigrateFunction`, `DispatcherFunction` — the dispatcher shares the migration image). The OCR worker image is not a SAM
function: it is the `OcrTaskDefinition`'s container image, given by its digest URI (`WorkerImageUri`).

**Complete order of a first deployment:** `build-and-push.sh` → `preflight.sh` → `deploy.sh pass1` →
`invoke-migration.sh migrate` (§6) → tenant and identity mapping (§7) → `deploy.sh pass2` → `smoke.sh` → the
authenticated checks (§8).

Run migrations **before** pass 2 (section 6), then close the loop:

```bash
./deploy.sh pass2        # sets the frontend origin as the Clerk authorized party and the S3 CORS origin
```

Why two passes: the frontend's own URL exists only after the stack does, and both FastAPI (Clerk authorized
party) and the bucket (CORS) must name it. Until pass 2 the API refuses every request (no authorized party) and S3
CORS is absent — fail closed. If Clerk requires allowed origins/redirect URLs, add the `FrontendUrl` origin in the
Clerk dashboard now.

Stack outputs (`aws cloudformation describe-stacks --stack-name ap-agent-production --query 'Stacks[0].Outputs'`):
`FrontendUrl`, `ApiUrl`, `UploadBucketName`, `QueueUrl`, `DispatcherFunctionArn`, `MigrationFunctionArn`, `OcrClusterArn`,
`OcrTaskDefinitionArn`, `OcrSubnetIds`, `OcrSecurityGroupId`, `OcrLogGroupName` (plus `DeadLetterQueueUrl`, `AlarmTopicArn`).

## 6. Migration invocation (exactly once, deliberate, fail-closed)

The migration function has **no Function URL and no trigger**; it is the only function that can read the migration
DSN. It applies the migrations under a PostgreSQL advisory lock, then grants the runtime role schema access (and
revokes its write access to the identity tables).

```bash
./invoke-migration.sh migrate        # prints {"ok": true, "migrations": [{"id": "0001…", "status": …}, …]}
```

It exits non-zero and changes nothing further if the DSN is missing or a migration checksum no longer matches.

> **Known first-deployment failure (fixed in M11E.2).** If this invocation reports
> `FileNotFoundError: …/site-packages/ap_agent/db/migrations/0001_memory_schema_bootstrap.sql`, the migration image was built
> from a commit that predates the packaging fix (the SQL files were not package data). Nothing touched the database. Update
> to the fixed commit and rebuild only that image: `git pull && ./build-and-push.sh migrate && ./preflight.sh && ./deploy.sh pass1`
> (or `pass2` if already past pass 1), then re-run `./invoke-migration.sh migrate`. Details: `docs/m11e2_fargate_ocr_correction.md` §9.
> To check an image yourself: `docker run --rm -v "$PWD/scripts/check_installed_migrations.py:/check.py:ro" --entrypoint python <migrate-image> /check.py --require-installed`.
Re-running is safe (applied migrations are skipped) but is only needed after an upgrade that adds migrations.

## 7. Register the production tenant and the first members

Uses the existing administrative CLI (`scripts/manage_identity_mappings.py`), executed *inside* the migration
function so the owner DSN never leaves AWS. Clerk IDs come from the Clerk dashboard (`org_…`, `user_…`).

```bash
./invoke-migration.sh identity register-tenant --tenant-key acme --display-name "Acme Ltd"
#   → prints the tenant UUID; use it below
./invoke-migration.sh identity register --tenant-id <tenant-uuid> --org-id org_... --user-id user_... --role AP_OPERATOR
./invoke-migration.sh identity register --tenant-id <tenant-uuid> --org-id org_... --user-id user_... --role AP_REVIEWER
./invoke-migration.sh identity register --tenant-id <tenant-uuid> --org-id org_... --user-id user_... --role READ_ONLY_AUDITOR
./invoke-migration.sh identity inspect --tenant-id <tenant-uuid> --json
```

Roles: `AP_OPERATOR`, `AP_REVIEWER`, `TENANT_ADMIN`, `READ_ONLY_AUDITOR`. Unmapped or inactive users are denied.
Disable a member with `identity disable --org-id … --user-id …`.

## 8. Smoke testing

```bash
./smoke.sh      # unauthenticated checks: redirect to sign-in, 401 for data calls, forged header refused,
                # API URL rejects unsigned/forged requests, bucket not public, queue settings, plus the OCR infrastructure:
                # task definition 4 vCPU / 8 GB, no ECS service, no task running while idle, dispatcher batch size 1,
                # no Lambda above 3,008 MB
```

Authenticated hosted acceptance (a person with an invited Clerk test user; uses the real deployment):

| # | Check | How |
|---|---|---|
| 1–2 | Public URL loads; anonymous visitors are redirected to Clerk sign-in | open `FrontendUrl` in a private window |
| 3 | A mapped organization member sees the interface | sign in as the operator |
| 4 | Auditor can view, cannot upload | sign in as the auditor: *Operations* shows no upload action / the API answers 403 `ACTION_NOT_PERMITTED` |
| 5 | Forged tenant/role/alternate headers fail | `smoke.sh` covers the unauthenticated forms (a forged `x-ap-agent-clerk-authorization` header with no session is **401** `AUTHENTICATION_REQUIRED`; **403** `ORGANIZATION_REQUIRED` is a signed-in user without an active organisation); with a valid session, headers added in browser dev-tools change nothing |
| 6 | Cross-organization access fails | as another organization's member, open the first tenant's job URL → 404 |
| 7–8 | A valid invoice (up to 10 MB) uploads via the staged S3 flow and the request returns without OCR | upload on *Operations*; the page shows *Accepted and queued* within seconds; the browser's network tab shows `POST …/upload-intents`, a direct POST to `*.amazonaws.com`, then `…/finalize` |
| 9–10 | SQS dispatches; a Fargate task runs real PaddleOCR and the message is deleted only after it exits 0 | `./observe-queue.sh` while the job runs: one RUNNING task, then a STOPPED task with `exit: 0`; the `/ecs/ap-agent-production-ocr` log shows `worker task: PROCESSED.`; the job timeline shows *Worker started* |
| 11–14 | Completion or review; claim → approve/correct → request resume; resume restarts at the recorded stage; original memory unchanged, derived version appended | follow the review queue; the job timeline for the resume job begins at the restart stage, not at ingestion |
| 15 | S3 objects private | `smoke.sh` (Block Public Access, unauthenticated GET 403); `aws s3api get-bucket-policy-status` |
| 16 | Duplicate queue delivery does not duplicate processing | `aws sqs send-message` the same body twice (§10 "replay a message"): the second is a no-op (`DUPLICATE_SKIPPED` in the log, one `JOB_CLAIMED` event) |
| 17 | No secret in HTML/JS/source maps/logs | view-source and DevTools search for `sk_`, `postgres://`, `AKIA`, `lambda-url`; `grep` the logs (§9) |
| 18 | Render/R2 mode unchanged | `render.yaml` and its tests are untouched; CI runs them |

## 9. Log inspection

```bash
aws logs tail /aws/lambda/ap-agent-production-api        --since 30m --follow
aws logs tail /aws/lambda/ap-agent-production-web        --since 30m
aws logs tail /aws/lambda/ap-agent-production-dispatcher --since 1h     # dispatch decisions: task started / completed / failed + exit code
aws logs tail /ecs/ap-agent-production-ocr               --since 1h     # the OCR task itself (stream prefix ocr/worker/<task-id>)
aws logs tail /aws/lambda/ap-agent-production-migrate    --since 1h
```

Retention is 14 days. Logs name settings and error classes, never DSNs, tokens, file names or object keys.
Useful filters: `--filter-pattern '"WORKER_INTERRUPTED"'`, `'"Unusable dispatch message"'`, `'"Dispatch failed"'`, `'"Job dispatch failed"'`.

## 10. Queue, task and DLQ inspection

```bash
./observe-queue.sh      # queue + DLQ depth, running tasks (expect at most 1), the last 5 stopped tasks with exit code / stop reason, recent logs
Q=$(aws cloudformation describe-stacks --stack-name ap-agent-production --query "Stacks[0].Outputs[?OutputKey=='QueueUrl'].OutputValue" --output text)
D=$(aws cloudformation describe-stacks --stack-name ap-agent-production --query "Stacks[0].Outputs[?OutputKey=='DeadLetterQueueUrl'].OutputValue" --output text)
aws sqs get-queue-attributes --queue-url "$Q" --attribute-names All --query 'Attributes.{waiting:ApproximateNumberOfMessages,inflight:ApproximateNumberOfMessagesNotVisible,visibility:VisibilityTimeout,redrive:RedrivePolicy}'
aws sqs get-queue-attributes --queue-url "$D" --attribute-names ApproximateNumberOfMessages
aws sqs receive-message --queue-url "$D" --max-number-of-messages 1 --visibility-timeout 30   # inspect a dead letter (contains only job id, tenant id, generation)
C=$(aws cloudformation describe-stacks --stack-name ap-agent-production --query "Stacks[0].Outputs[?OutputKey=='OcrClusterArn'].OutputValue" --output text)
aws ecs list-tasks --cluster "$C" --desired-status RUNNING
aws ecs describe-tasks --cluster "$C" --tasks <task-arn> --query 'tasks[].{status:lastStatus,stop:stopCode,reason:stoppedReason,exit:containers[0].exitCode}'
```

**Exit codes of the OCR task** (the dispatcher acknowledges the message only for `0`):

| Exit | Meaning | Result |
|---|---|---|
| 0 | handled: the job was processed (its outcome — completed, review required or failed — is recorded in PostgreSQL) or the message was a harmless duplicate | message deleted |
| 4 | configuration error (secret/SSM/worker configuration) | dispatcher raises → retry with back-off → DLQ after 3 receives |
| 5 | the OCR provider could not be initialised | same |
| 6 | unusable message (malformed, unknown job, tenant mismatch) | same (and the dispatcher never starts a task for a malformed body) |
| 7 | transient failure (database/storage) | same |
| 8 | hard deadline (`OcrTaskMaxSeconds`, default 720 s) reached; the job stays `RUNNING` and the next delivery fails it as `WORKER_INTERRUPTED` | same |
| none | the task never started (e.g. `CannotPullContainerError`, no capacity) or was killed (out of memory = 137) | same |

* **What lands in the DLQ:** a message that failed three times. A job is **never** re-run automatically: the task claims
  the job `QUEUED → RUNNING` atomically; a redelivery of a job that is no longer `QUEUED` does nothing (exit 0), and a job
  found `RUNNING` on a redelivery is failed (`WORKER_INTERRUPTED`) rather than silently re-run. A person re-submits it.
* **Blocked queue protection.** The queue has one FIFO message group, so a failed message would block every later job
  until it becomes visible again. The dispatcher therefore shortens the visibility of a failed message to 60 s / 300 s /
  900 s (1st / 2nd / 3rd failure) instead of the queue's 5,400 s crash-recovery timeout. If the dispatcher itself dies,
  the 5,400 s timeout applies.
* **Redrive** a dead letter after fixing the cause: *SQS console → DLQ → Start DLQ redrive*, or
  `aws sqs start-message-move-task --source-arn <dlq-arn>`.
* **Replay a message** (e.g. to prove idempotency): `aws sqs send-message --queue-url "$Q" --message-group-id ap-agent-jobs --message-deduplication-id "test-$(date +%s)" --message-body '<same JSON body>'`.
* **A job stuck in QUEUED** (dispatch failed after the job was saved, or all three attempts failed before the claim): the
  upload request answered 503 `DISPATCH_UNAVAILABLE`; repeating the same upload/finalize call re-dispatches it without
  creating a second job.
* Alarms (SNS topic `ap-agent-production-alarms`): DLQ not empty, oldest message > 30 min, dispatcher errors, a job
  > 13 min end to end, API/web errors.

### Troubleshooting: *Backend unavailable* with an empty API log group

The frontend loads and signs you in, but every data call fails and `/aws/lambda/ap-agent-production-api` has no events: the call
never reached the API. A Function URL needs **both** `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` on the web role
(`InvokeApiFunctionUrl` and `InvokeApiFunctionViaUrl` in `template.yaml`); check with
`aws iam get-role-policy --role-name <WebRole> --policy-name web`, and redeploy with `./deploy.sh pass2` if the second statement is missing.

### Troubleshooting: `unsupported startup parameter in options: statement_timeout`

Every API call (including `/health/ready`) fails and the API log shows `psycopg.OperationalError … unsupported startup parameter in
options`. The runtime DSN points at Neon's **pooled** endpoint, which refuses startup options; images built before M11E.3 send
`statement_timeout`/`lock_timeout` that way. Rebuild `api`, `migrate` and `worker` from a commit that includes
`src/ap_agent/db/connection.py` as of M11E.3 (it applies the limits transaction-locally on pooled endpoints) and redeploy — exact
steps in `docs/m11e2_fargate_ocr_correction.md` §11. Do **not** "fix" this by switching the runtime DSN to the direct endpoint.

### Troubleshooting the Fargate path

| Symptom | Look at | Typical cause |
|---|---|---|
| Dispatcher error `RUN_TASK_FAILED:<Exception>` | `aws logs tail /aws/lambda/ap-agent-production-dispatcher` | missing IAM (`ecs:RunTask`, `iam:PassRole`), wrong cluster/task-definition variables |
| `PLACEMENT_FAILED:RESOURCE:…` / `CAPACITY` | RunTask failure reason in the dispatcher log | Fargate vCPU quota below 4, or no capacity in the AZ (the task spreads over two subnets) |
| `NO_EXIT_CODE:TaskFailedToStart:CannotPullContainerError` | `describe-tasks … stoppedReason` | image URI/digest wrong, image deleted by the ECR lifecycle (only the last 3 are kept), execution role cannot pull, task has no route to ECR (public IP/internet gateway/route) |
| exit 4 | `/ecs/…-ocr` log: `configuration error (<Class>)` | SSM parameter missing, task role cannot `ssm:GetParameter` the runtime DSN, no AWS region |
| exit 5 | `/ecs/…-ocr` log | PaddleOCR could not initialise (image/model cache) |
| exit 7 | `/ecs/…-ocr` log: `transient failure (<Class>)` | Neon unreachable from the task (egress 5432/443), database credentials |
| exit 137 | stop reason `OutOfMemory` | a document exceeded 8 GB — investigate the document; do not raise Lambda memory (not applicable) |
| `DISPATCHER_DEADLINE` | dispatcher log | image pull + OCR exceeded ~14 minutes: lower `OcrTaskMaxSeconds`, check pull time |
| Messages waiting, no task | `observe-queue.sh`, dispatcher ESM state | event-source mapping disabled, a failed message in back-off (visibility 60–900 s) |

## 11. Redeployment (new code or secret rotation)

```bash
git pull
./build-and-push.sh        # new image tag = the git commit
./deploy.sh pass2          # same parameters, new images
./invoke-migration.sh migrate   # only if the release adds migrations
./smoke.sh
```

CloudFormation updates the functions and registers a new task-definition revision in place; the dispatcher starts every
new task from the stack's current revision. An OCR task already running finishes on the old revision (SQS redelivers
anything interrupted).

**Rebuild only what changed** (each image is independent; the other recorded URIs are kept in `images.env`):

```bash
./build-and-push.sh worker            # OCR / worker code only → then ./deploy.sh pass2
./build-and-push.sh migrate           # migration handler or dispatcher code (the dispatcher uses this image)
./build-and-push.sh api               # backend API
CLERK_PUBLISHABLE_KEY=pk_... ./build-and-push.sh web     # frontend
./preflight.sh && ./deploy.sh pass2
```

## 12. Rollback

1. **Application code:** re-run `build-and-push.sh` from the previous commit (or edit `deploy/aws/.local/images.env`
   to the previous image URIs — the last 3 images are kept) and `./deploy.sh pass2`. Lambda switches over atomically.
2. **A bad CloudFormation change:** it rolls back automatically; to roll back a *successful* one, redeploy the previous
   commit's template.
3. **Database:** migrations are additive and not reversed by this runbook. The M8–M11 schema is append-only; restore from
   a Neon branch/point-in-time restore if ever needed.
4. **Emergency stop without deleting anything:** disable the dispatcher trigger
   (`aws lambda update-event-source-mapping --uuid <id> --no-enabled`; list it with `aws lambda list-event-source-mappings --function-name ap-agent-production-dispatcher`),
   stop any running OCR task (`aws ecs stop-task --cluster <OcrClusterArn> --task <arn>`; its job is failed as interrupted on redelivery),
   and/or set the API reserved concurrency to 0 (`aws lambda put-function-concurrency --function-name ap-agent-production-api --reserved-concurrent-executions 0`).
   Messages wait in the queue (4 days).
5. **A failed first deployment** (stack in `ROLLBACK_COMPLETE`): it cannot be updated — delete the *stack only*
   (`./teardown.sh --stack-only`, below), keep the ECR images and SSM secrets, fix the cause and run `./preflight.sh` and
   `./deploy.sh pass1` again.

## 13. Complete teardown (stops all AWS charges for this deployment)

```bash
./teardown.sh --stack-only   # application stack ONLY: keeps the ECR stack, the images and the SSM secrets (use after a failed deployment)
./teardown.sh            # type the environment name to confirm; empties the bucket, deletes both stacks, images, secrets
./verify-teardown.sh     # exits 0 only when nothing remains
```

Stack-only equivalent without the script (what `preflight.sh` prints for a `ROLLBACK_COMPLETE` stack):
`aws cloudformation delete-stack --stack-name ap-agent-production && aws cloudformation wait stack-delete-complete --stack-name ap-agent-production`
(the invoice bucket is empty on a failed first deployment; the script empties it otherwise). This never touches the `ap-agent-production-ecr` stack or `/ap-agent/production/*`.

`teardown.sh` removes: the application stack (functions, URLs, queues, bucket, roles, log groups, alarms, topic), the
ECR stack and every image, the SSM parameters and (optionally) the shared SAM deployment bucket. It prints
`verify-teardown.sh`'s result: each of *stacks, Lambda functions, SQS queues, S3 buckets, ECR repositories, log groups
(Lambda and `/ecs`), SSM parameters, CloudWatch alarms, ECS clusters and tasks, VPCs* reports `gone`.

**Verify that costs stopped:** (1) `verify-teardown.sh` is clean; (2) next day, *Billing → Cost Explorer* (group by
Service, daily) shows no Lambda/S3/SQS/ECR/CloudWatch charges for the project tag `Project=accounts-payable-agent`;
(3) *Billing → Budgets* shows the monthly budget forecast near zero. Items **outside** AWS that you may still pay for or
keep: the Neon project and the Clerk application — delete them in their dashboards to stop those too. The AWS budget
and its alerts are not touched.

## 14. The Fargate OCR task — design, limits and boundaries

* **One invoice per task.** The dispatcher Lambda runs `ecs:RunTask` (Fargate, `count: 1`, `assignPublicIp: ENABLED`,
  no capacity provider, no service) with the job message as a container environment override and waits for the task to
  reach `STOPPED`. It returns — so Lambda deletes the SQS message — **only** when the `worker` container exited `0`;
  everything else raises (retry → DLQ). Task *creation* is never the success boundary.
* **Single active task.** One FIFO message group + batch size 1: SQS delivers the next message only after the previous one
  is deleted or becomes visible again. The `QUEUED → RUNNING` database claim is the second guard against double execution.
* **Time budget.** The dispatcher Lambda has a 900 s timeout. It stops the task and fails when less than 60 s remain.
  The container's own deadline is 720 s (`OcrTaskMaxSeconds`), leaving ~2 minutes for the image pull and start-up. The
  benchmark gate is 12 minutes per invoice. A job that needs longer is not supported by this design (see the report).
* **Network.** One VPC (10.42.0.0/24), two public subnets, an internet gateway, **no NAT gateway, no endpoints, no load
  balancer**. The task security group has **no inbound rule**; outbound is TCP 443 (ECR, S3, SSM, Neon) and 5432 (Neon).
  The task gets a public IPv4 only while it runs.
* **IAM.** *Dispatcher role*: `ecs:RunTask` on this task-definition family and cluster only; `DescribeTasks`/`StopTask` on
  this cluster's tasks only; `iam:PassRole` for exactly the two OCR roles and only to `ecs-tasks.amazonaws.com`; queue
  consume + `ChangeMessageVisibility`; its own log group. No S3, no SSM. *Execution role*: ECR pull from the worker
  repository, log writes, no secrets. *Task role*: `ssm:GetParameter` on the runtime DSN only, `s3:GetObject` on
  `tenants/*` (read-only), `s3:ListBucket`. Nobody but the dispatcher can start tasks or pass roles.
* **Secrets** are never in the task definition or the override: the process reads the runtime DSN from SSM with its task role.

## 15. Estimated monthly cost (low volume) and what could consume the USD 20 budget

Assumptions: 20 invoices/month, ~3 min of Fargate time each (image pull + start-up + ~100 s OCR), ~1,000 page views/API calls.
Prices are approximate published on-demand eu-west-2 rates (Fargate Linux/x86 ≈ USD 0.0466 per vCPU-hour and ≈ USD 0.0051
per GB-hour, public IPv4 ≈ USD 0.005 per hour) — re-check them before relying on the figures.

| Component | Estimate / month | Notes |
|---|---|---|
| Fargate — OCR task (4 vCPU / 8 GB) | **~USD 0.20–1** | ≈ USD 0.23/hour ≈ USD 0.004/minute; a 12-minute job ≈ USD 0.05; 100 such jobs ≈ USD 5; **~400 worst-case 12-minute jobs ≈ USD 18** (the point where the OCR alone approaches the budget) |
| Public IPv4 of the task | < USD 0.05 | billed per hour only while the task runs |
| Lambda — dispatcher (256 MB, waits for the task) | < USD 0.10 | 12 min × 0.25 GB ≈ 180 GB-s per job ≈ USD 0.003 |
| Lambda — frontend + API (1024 MB) | ~USD 0.10–0.60 | free tier covers the first 400,000 GB-s |
| SQS, SNS, CloudWatch alarms | ~USD 0.50 | 8 alarms ≈ USD 0.80 beyond the free 10; 14-day logs |
| S3 | < USD 0.10 | 1-day staging expiry; invoices kept |
| ECR | < USD 0.20 | 4 repos, 3 images each (the worker image is multi-GB: ~USD 0.10/GB-month) |
| CloudWatch Logs | < USD 0.30 | 14-day retention |
| VPC, subnets, internet gateway, security group, ECS cluster | USD 0 | no NAT gateway, no endpoints: nothing billed while idle |
| SSM Parameter Store (standard), Function URLs | USD 0 | |
| **Total (AWS)** | **≈ USD 1–3** | Neon and Clerk are billed by their own plans (free tiers cover the MVP) |

**Most likely to consume the budget:** (1) Fargate time if volume grows or a retry loop runs (bounded: one FIFO message
group means one job at a time; 3 receives then DLQ — at most 3 × 15 minutes per poison job); (2) **ECR storage of the multi-GB
worker image**; (3) CloudWatch alarms/logs; (4) data-transfer-out to Neon (small); (5) **a mistakenly added NAT gateway or
ECS service** (both are forbidden by the template policy test). Nothing here is billed while idle except ECR storage and alarms.

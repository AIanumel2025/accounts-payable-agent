# M11E.1 runbook — AWS deployment (Lambda + SQS + S3, Neon, Clerk)

Operator procedure for the cost-optimised AWS deployment (region **eu-west-2**). It deploys the unchanged
application as scale-to-zero Lambda functions; the Render/R2 deployment in `docs/m11e_deployment_runbook.md`
keeps working side by side. Everything here uses **placeholders**. Never paste a secret into a file, a commit, a PR,
a log or chat: secrets are typed only at the hidden prompts of `create-secrets.sh` (or the provider dashboards).

```
browser ──► frontend Lambda (public Function URL, Clerk sign-in) ──SigV4──► API Lambda (AWS_IAM Function URL)
   │                                                                          │  ├─► Neon PostgreSQL (runtime role)
   └─ presigned POST ─► private S3 bucket  ◄── API: validates + finalizes ────┘  └─► SQS FIFO ─► worker Lambda
                                                                                   (PaddleOCR; Neon + S3 read-only)
migration Lambda (no URL; invoked by an operator) ─► Neon (migration/owner role)
```

## 1. Prerequisites (one-time, before the first deployment)

| Need | How |
|---|---|
| AWS credentials | **AWS CloudShell** (recommended) or a workstation signed in with IAM Identity Center (`aws sso login`). Never create long-lived access keys. The identity needs permission to manage CloudFormation, Lambda, IAM roles, S3, SQS, SSM, ECR, CloudWatch and SNS in the account. |
| Budget | The USD 20/month budget and its 50/80/100 % alerts already exist. **Do not create another.** |
| Tools | `aws` v2, `docker`, `sam` (AWS SAM CLI), `jq`, `git`. CloudShell has `aws`, `docker` and `git`; install SAM and jq per section 3. |
| Lambda concurrency quota | New accounts often start at **10**, which blocks any reserved concurrency (the worker reserves 1 and Lambda insists 10 stay unreserved). `preflight.sh` checks it; if it is below 11, request an increase in *Service Quotas → AWS Lambda → Concurrent executions* (code `L-B99A9384`, e.g. 100) and wait for approval. With a quota between 11 and 30, deploy with `WEB_API_RESERVED_CONCURRENCY=-1`. |
| Clerk | Application with **Organizations** on, user-created organizations **off**, sign-up **invitation-only**, the organization created and the pilot users invited (see `docs/m11e_deployment_runbook.md` §2). Note the **publishable key** (`pk_…`, public), the **secret key** (`sk_…`, secret) and the **issuer URL** (`https://<instance>.clerk.accounts.dev`). |
| Neon | Existing project with two roles: a **migration/owner** role (direct endpoint DSN) and a **least-privilege runtime** role (`LOGIN`, no `SUPERUSER`, no `BYPASSRLS`; pooled endpoint DSN). Both with `sslmode=require`. They must be different roles. |
| Disk for images | The worker image is several GB. CloudShell's Docker may run out of space: if `docker build` fails with "no space left", build on any machine with Docker and an AWS session instead (`build-and-push.sh` works unchanged anywhere). |

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
./preflight.sh        # masks the account id; checks region, tools, Lambda quota and which secrets exist
```

## 4. Secrets (SSM Parameter Store, SecureString)

```bash
./create-secrets.sh
```

It asks, with hidden input, for: the Neon **runtime** DSN, the Neon **migration** DSN and the Clerk **secret** key. It
generates the frontend CSRF secret itself. Values are stored as SecureStrings under `/ap-agent/production/`:

| Parameter | Read by |
|---|---|
| `postgres-runtime-dsn` | API function, worker function |
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
# export WORKER_MEMORY_MB=8192                           # default (benchmarked); the Lambda maximum is 10240
# export WEB_API_RESERVED_CONCURRENCY=-1                 # only if the quota is 11-30

./build-and-push.sh      # creates the ECR stack (lifecycle: keep 3 images), builds + pushes 4 images
./deploy.sh pass1        # creates the application stack; prints the public FrontendUrl
```

Run migrations **before** pass 2 (section 6), then close the loop:

```bash
./deploy.sh pass2        # sets the frontend origin as the Clerk authorized party and the S3 CORS origin
```

Why two passes: the frontend's own URL exists only after the stack does, and both FastAPI (Clerk authorized
party) and the bucket (CORS) must name it. Until pass 2 the API refuses every request (no authorized party) and S3
CORS is absent — fail closed. If Clerk requires allowed origins/redirect URLs, add the `FrontendUrl` origin in the
Clerk dashboard now.

Stack outputs (`aws cloudformation describe-stacks --stack-name ap-agent-production --query 'Stacks[0].Outputs'`):
`FrontendUrl`, `ApiUrl`, `UploadBucketName`, `QueueUrl`, `WorkerFunctionArn`, `MigrationFunctionArn`
(plus `DeadLetterQueueUrl`, `AlarmTopicArn`).

## 6. Migration invocation (exactly once, deliberate, fail-closed)

The migration function has **no Function URL and no trigger**; it is the only function that can read the migration
DSN. It applies the migrations under a PostgreSQL advisory lock, then grants the runtime role schema access (and
revokes its write access to the identity tables).

```bash
./invoke-migration.sh migrate        # prints {"ok": true, "migrations": [{"id": "0001…", "status": …}, …]}
```

It exits non-zero and changes nothing further if the DSN is missing or a migration checksum no longer matches.
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
                # API URL rejects unsigned/forged requests, bucket not public, queue settings
```

Authenticated hosted acceptance (a person with an invited Clerk test user; uses the real deployment):

| # | Check | How |
|---|---|---|
| 1–2 | Public URL loads; anonymous visitors are redirected to Clerk sign-in | open `FrontendUrl` in a private window |
| 3 | A mapped organization member sees the interface | sign in as the operator |
| 4 | Auditor can view, cannot upload | sign in as the auditor: *Operations* shows no upload action / the API answers 403 `ACTION_NOT_PERMITTED` |
| 5 | Forged tenant/role/alternate headers fail | `smoke.sh` covers the unauthenticated forms; with a valid session, headers added in browser dev-tools change nothing |
| 6 | Cross-organization access fails | as another organization's member, open the first tenant's job URL → 404 |
| 7–8 | A valid invoice (up to 10 MB) uploads via the staged S3 flow and the request returns without OCR | upload on *Operations*; the page shows *Accepted and queued* within seconds; the browser's network tab shows `POST …/upload-intents`, a direct POST to `*.amazonaws.com`, then `…/finalize` |
| 9–10 | SQS dispatches; the worker runs real PaddleOCR | §10: the worker log shows `Job … finished as …`; the job timeline shows *Worker started* |
| 11–14 | Completion or review; claim → approve/correct → request resume; resume restarts at the recorded stage; original memory unchanged, derived version appended | follow the review queue; the job timeline for the resume job begins at the restart stage, not at ingestion |
| 15 | S3 objects private | `smoke.sh` (Block Public Access, unauthenticated GET 403); `aws s3api get-bucket-policy-status` |
| 16 | Duplicate queue delivery does not duplicate processing | `aws sqs send-message` the same body twice (§10 "replay a message"): the second is a no-op (`DUPLICATE_SKIPPED` in the log, one `JOB_CLAIMED` event) |
| 17 | No secret in HTML/JS/source maps/logs | view-source and DevTools search for `sk_`, `postgres://`, `AKIA`, `lambda-url`; `grep` the logs (§9) |
| 18 | Render/R2 mode unchanged | `render.yaml` and its tests are untouched; CI runs them |

## 9. Log inspection

```bash
aws logs tail /aws/lambda/ap-agent-production-api    --since 30m --follow
aws logs tail /aws/lambda/ap-agent-production-web    --since 30m
aws logs tail /aws/lambda/ap-agent-production-worker --since 1h
aws logs tail /aws/lambda/ap-agent-production-migrate --since 1h
```

Retention is 14 days. Logs name settings and error classes, never DSNs, tokens, file names or object keys.
Useful filters: `--filter-pattern '"WORKER_INTERRUPTED"'`, `'"Unusable dispatch message"'`, `'"Job dispatch failed"'`.

## 10. Queue and DLQ inspection

```bash
Q=$(aws cloudformation describe-stacks --stack-name ap-agent-production --query "Stacks[0].Outputs[?OutputKey=='QueueUrl'].OutputValue" --output text)
D=$(aws cloudformation describe-stacks --stack-name ap-agent-production --query "Stacks[0].Outputs[?OutputKey=='DeadLetterQueueUrl'].OutputValue" --output text)
aws sqs get-queue-attributes --queue-url "$Q" --attribute-names All --query 'Attributes.{waiting:ApproximateNumberOfMessages,inflight:ApproximateNumberOfMessagesNotVisible,visibility:VisibilityTimeout,redrive:RedrivePolicy}'
aws sqs get-queue-attributes --queue-url "$D" --attribute-names ApproximateNumberOfMessages
aws sqs receive-message --queue-url "$D" --max-number-of-messages 1 --visibility-timeout 30   # inspect a dead letter (contains only job id, tenant id, generation)
```

* **What lands in the DLQ:** a message that failed three times: malformed/unknown-job/tenant-mismatch ("poison") or a
  transient infrastructure error that persisted (database or storage down). A job is **never** re-run
  automatically: every job runs in a fresh child process; if that process is killed (out of memory), crashes or runs
  out of time, the handler marks the job `FAILED` with `WORKER_PROCESS_FAILED` / `WORKER_TIMEOUT` straight away
  (`WORKER_INTERRUPTED` if the whole invocation died), and a person re-submits the invoice.
* **Redrive** a dead letter after fixing the cause: *SQS console → DLQ → Start DLQ redrive*, or
  `aws sqs start-message-move-task --source-arn <dlq-arn>`.
* **Replay a message** (e.g. to prove idempotency): `aws sqs send-message --queue-url "$Q" --message-group-id ap-agent-jobs --message-deduplication-id "test-$(date +%s)" --message-body '<same JSON body>'`.
* **A job stuck in QUEUED** (dispatch failed after the job was saved): the upload request answered 503
  `DISPATCH_UNAVAILABLE`; repeating the same upload/finalize call re-dispatches it without creating a second job.
* Alarms (SNS topic `ap-agent-production-alarms`): DLQ not empty, oldest message > 30 min, worker errors, worker run
  > 12 min, API/web errors.

## 11. Redeployment (new code or secret rotation)

```bash
git pull
./build-and-push.sh        # new image tag = the git commit
./deploy.sh pass2          # same parameters, new images
./invoke-migration.sh migrate   # only if the release adds migrations
./smoke.sh
```

CloudFormation updates the functions in place; in-flight worker runs finish (SQS redelivers anything interrupted).

## 12. Rollback

1. **Application code:** re-run `build-and-push.sh` from the previous commit (or edit `deploy/aws/.local/images.env`
   to the previous image URIs — the last 3 images are kept) and `./deploy.sh pass2`. Lambda switches over atomically.
2. **A bad CloudFormation change:** it rolls back automatically; to roll back a *successful* one, redeploy the previous
   commit's template.
3. **Database:** migrations are additive and not reversed by this runbook. The M8–M11 schema is append-only; restore from
   a Neon branch/point-in-time restore if ever needed.
4. **Emergency stop without deleting anything:** disable the worker trigger
   (`aws lambda update-event-source-mapping --uuid <id> --no-enabled`; list it with `aws lambda list-event-source-mappings --function-name ap-agent-production-worker`)
   and/or set the API reserved concurrency to 0 (`aws lambda put-function-concurrency --function-name ap-agent-production-api --reserved-concurrent-executions 0`).
   Messages wait in the queue (4 days).

## 13. Complete teardown (stops all AWS charges for this deployment)

```bash
./teardown.sh            # type the environment name to confirm; empties the bucket, deletes both stacks, images, secrets
./verify-teardown.sh     # exits 0 only when nothing remains
```

`teardown.sh` removes: the application stack (functions, URLs, queues, bucket, roles, log groups, alarms, topic), the
ECR stack and every image, the SSM parameters and (optionally) the shared SAM deployment bucket. It prints
`verify-teardown.sh`'s result: each of *stacks, Lambda functions, SQS queues, S3 buckets, ECR repositories, log groups,
SSM parameters, CloudWatch alarms* reports `gone`.

**Verify that costs stopped:** (1) `verify-teardown.sh` is clean; (2) next day, *Billing → Cost Explorer* (group by
Service, daily) shows no Lambda/S3/SQS/ECR/CloudWatch charges for the project tag `Project=accounts-payable-agent`;
(3) *Billing → Budgets* shows the monthly budget forecast near zero. Items **outside** AWS that you may still pay for or
keep: the Neon project and the Clerk application — delete them in their dashboards to stop those too. The AWS budget
and its alerts are not touched.

## 14. Fargate fallback (only if Lambda cannot run PaddleOCR in time)

`deploy/aws/fargate-fallback.yaml` runs one on-demand ECS Fargate task per queue message (EventBridge Pipes), with no
service, NAT gateway or load balancer. Use it only if the benchmark (`docs/m11e1_aws_go_live_report.md`) shows the
Lambda worker exceeds 12 minutes or its image exceeds 10 GB. Steps: build the existing `docker/worker.Dockerfile` image
and push it to the `worker` repository; disable the Lambda event-source mapping (above); deploy the template with
the default-VPC public subnet ids, a security group with no inbound rules and the `QueueUrl`'s queue ARN. Limitations:
Pipes deletes the message when the task is launched, so a crashed task leaves the job `RUNNING` (mark it failed by
re-submitting); several tasks may run concurrently on *different* jobs (the claim is atomic per job, so a job never runs
twice).

## 15. Estimated monthly cost (low volume) and what could consume the USD 20 budget

Assumptions: 20 invoices/month, ~2 min of OCR each, ~1,000 page views/API calls.

| Component | Estimate / month | Notes |
|---|---|---|
| Lambda — worker (8192 MB) | **~USD 0.30–0.90** | 20 × 100 s × 8 GB = 16,000 GB-s ≈ USD 0.27; scales linearly: 1,000 invoices ≈ USD 13 |
| Lambda — frontend + API (1024 MB) | ~USD 0.10–0.60 | free tier covers the first 400,000 GB-s |
| SQS, SNS, CloudWatch alarms | ~USD 0.50 | 8 alarms ≈ USD 0.80 beyond the free 10; 14-day logs |
| S3 | < USD 0.10 | 1-day staging expiry; invoices kept |
| ECR | < USD 0.20 | 4 repos, 3 images each (the worker image is multi-GB: ~USD 0.10/GB-month) |
| CloudWatch Logs | < USD 0.30 | 14-day retention |
| SSM Parameter Store (standard) | USD 0 | |
| Public Function URLs | USD 0 | |
| **Total (AWS)** | **≈ USD 1–3** | Neon and Clerk are billed by their own plans (free tiers cover the MVP) |

**Most likely to consume the budget:** (1) the OCR worker if volume grows or a retry loop runs (bounded: reserved
concurrency 1, 3 receives then DLQ); (2) **ECR storage of the multi-GB worker image**; (3) CloudWatch alarms/logs;
(4) data-transfer-out from Lambda to Neon (small). Nothing here is billed while idle except ECR storage and alarms.

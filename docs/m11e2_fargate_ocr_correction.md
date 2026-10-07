# M11E.2 — AWS deployment correction: OCR moves from Lambda to on-demand ECS Fargate

**Status: corrected and tested locally; NOT deployed.** No stack exists, no migration ran, no identity mapping was
registered and no authenticated smoke test was performed. This milestone must not be described as deployed until a real
pass 1, the migration, the identity mapping, pass 2 and the authenticated smoke tests (runbook §5–§8) have succeeded.
The PR stays open and unmerged. No AWS call was made while authoring this change.

## 1. Why

The first real deployment (M11E.1) failed at the worker function:

```
'MemorySize' ... must be less than or equal to 3008
```

CloudFormation rolled the stack back; the ECR images and SSM parameters remain. This account limits Lambda to 3,008 MB.
The benchmark (`docs/m11e1_aws_go_live_report.md` §6) measured per-job peak resident sets up to ~6.6–6.8 GB, so a
3,008 MB worker would be OOM-killed. **Reducing the worker to 3,008 MB is not an option and was not done**; the worker
keeps ≥ 8 GB.

## 2. Changed architecture

```
SQS FIFO (1 message group, batch 1) ─► dispatcher Lambda (256 MB, MigrateImage, no OCR, no secrets)
                                          └─ ecs:RunTask ×1 ─► Fargate task 4 vCPU / 8 GB ("worker", WorkerImageUri by digest)
                                          └─ poll DescribeTasks until STOPPED; exit code 0 ⇒ return (Lambda deletes the message)
                                                                otherwise ⇒ raise (SQS retry → FIFO DLQ after 3 receives)
```

| Area | Before (M11E.1) | After (M11E.2) |
|---|---|---|
| OCR runtime | `WorkerFunction` Lambda, 8192 MB (rejected by the account) | `OcrTaskDefinition` Fargate task, 4096 CPU units / 8192 MB, 30 GiB ephemeral storage, `EntryPoint [python,-m]`, `Command [ap_agent.worker.dispatch_task]` |
| Queue consumer | the worker | `DispatcherFunction` (256 MB, 900 s, batch size 1, same migration image, `ImageConfig.Command ap_agent.aws.fargate_dispatcher.handler`) |
| Success boundary | Lambda returning | the task **STOPPED with container exit code 0** (task creation is not success) |
| Network | no VPC | one VPC, two public subnets, internet gateway; **no NAT gateway, no endpoints, no load balancer**; task security group with **no inbound rule**, egress TCP 443 and 5432 only |
| Logs | `/aws/lambda/…-worker` | `/ecs/ap-agent-<env>-ocr` (+ `/aws/lambda/…-dispatcher`), 14-day retention |
| Alarms | worker errors / duration | dispatcher errors / duration (> 13 min end to end) |
| Outputs | `WorkerFunctionArn` | `DispatcherFunctionArn`, `OcrClusterArn`, `OcrTaskDefinitionArn`, `OcrSubnetIds`, `OcrSecurityGroupId`, `OcrLogGroupName` |
| `fargate-fallback.yaml` | optional EventBridge-Pipes fallback | **deleted** (superseded; the Pipes variant deleted the message at launch) |

Not present, by design and by test: ECS service, NAT gateway, ALB/NLB, RDS, VPC endpoints, EIP, capacity provider,
provisioned concurrency, always-on compute, Lambda memory above 3,008 MB.

### Dispatcher behaviour (`src/ap_agent/aws/fargate_dispatcher.py`)

1. Refuses any batch other than one record (`BATCH_SIZE_INVALID`), a malformed message or another message group — before
   starting anything.
2. `ecs:RunTask` (Fargate, count 1, `assignPublicIp ENABLED`, two subnets, the OCR security group). The **only** override is
   the container environment `AP_AGENT_DISPATCH_MESSAGE` (job id, tenant id, generation) and
   `AP_AGENT_DISPATCH_RECEIVE_COUNT`. No secret, command, memory or role override.
3. A `failures` entry, zero or several tasks → `FargateStartError`. Never waits on a task that did not start.
4. Polls `DescribeTasks` (10 s) until `STOPPED`. Three consecutive describe failures → stop the task, `FargateWaitError`.
5. `STOPPED` with exit code ≠ 0, a missing exit code (pull failure, killed) or another container's code →
   `FargateTaskFailedError`.
6. With < 60 s of the Lambda's 900 s left it calls `StopTask` and raises `FargateTimeoutError`.
7. Before raising it shortens the failed message's visibility (60 s / 300 s / 900 s by receive count) so a one-group FIFO
   queue is not blocked for the queue's 5,400 s crash-recovery timeout. Best effort; never masks the real error.
8. Never deletes the message itself and logs only identifiers and codes.

### Task behaviour (`src/ap_agent/worker/dispatch_task.py`)

Validates the message first (exit 6, nothing else started), starts a self-imposed hard deadline (`AP_AGENT_TASK_MAX_SECONDS`,
exit 8), reads the runtime DSN from SSM through its task role, builds the production runner and calls the same
`process_dispatch_record` as the former Lambda: atomic `QUEUED → RUNNING` claim, duplicate delivery = no-op (exit 0),
orphaned `RUNNING` job on a redelivery = failed `WORKER_INTERRUPTED`. Exit codes: 0 handled, 4 configuration, 5 OCR
unavailable, 6 unusable message, 7 transient, 8 deadline.

## 3. Security and IAM boundaries

* **DispatcherRole**: `ecs:RunTask` on the task-definition family `ap-agent-<env>-ocr:*` with `ecs:cluster` equal to the
  cluster; `ecs:DescribeTasks`/`ecs:StopTask` on this cluster's tasks; `iam:PassRole` on exactly the execution and task
  role, `iam:PassedToService = ecs-tasks.amazonaws.com`; queue consume + `ChangeMessageVisibility`; its log group. No S3, no SSM.
* **OcrExecutionRole**: `ecr:GetAuthorizationToken` (the one unavoidable `*`), image pull from the worker repository only,
  writes to the OCR log group. No secrets.
* **OcrTaskRole**: `ssm:GetParameter` on the runtime DSN only; `s3:GetObject` on `tenants/*`; `s3:ListBucket`. Never the
  migration DSN, never S3 writes, no queue, no ECS, no PassRole.
* Both ECS roles trust `ecs-tasks.amazonaws.com` with `aws:SourceAccount`. Secrets are never in the task definition, an ECS
  `Secrets` block, an override, an image layer or a log. Image URIs must match an ECR URI pattern (tag or `@sha256:` digest).
* All of this is enforced by `scripts/verify_aws_templates.py` and mutation-tested in `tests/unit/test_m11e1_aws_templates.py`.

## 4. Deployment-script corrections

* `deploy.sh`: pass 1 no longer passes `FrontendOrigin` (nor an empty `AlarmEmail`); `WorkerMemoryMb` is gone; explicit
  `--image-repositories` for `ApiFunction`, `WebFunction`, `MigrateFunction`, `DispatcherFunction` (the dispatcher shares
  the migration repository); the worker image is the task definition's, by digest.
* `build-and-push.sh`: records **digest** URIs; `build-and-push.sh worker` (or any subset) rebuilds only those images and
  keeps the other recorded URIs.
* `preflight.sh`: blocking — every Lambda ≤ 3,008 MB (template check), Fargate On-Demand vCPU quota ≥ 4 (when readable), a
  stack in `ROLLBACK_COMPLETE`/`CREATE_FAILED`/`*_IN_PROGRESS`, missing images, missing SSM parameters; informational —
  Lambda concurrency quota, VPC count, unreadable quota. Existence checks only; never decrypts a parameter, never prints an
  account id (masked).
* `smoke.sh`: adds the Fargate/infrastructure checks. New `observe-queue.sh`. `teardown.sh --stack-only` deletes only the
  application stack (ECR stack, images and SSM secrets kept); `verify-teardown.sh` also checks ECS clusters/tasks and VPCs.
* CI (`.github/workflows/m11e1-aws.yml`): `cfn-lint`, `sam validate --lint` (placeholder credentials, never used against
  AWS), the policy verifier, the 3,008 MB check, the template and script tests, the dispatcher smoke in the migration image,
  the `dispatch_task` exit-6 smoke in the worker image, and the unchanged real-PaddleOCR benchmark under 4 vCPU / 8 GB.

## 5. Tests and exact results

| Check | Result |
|---|---|
| Full backend suite (`pytest -m "not requires_paddle" --ignore=tests/unit/test_paddleocr_adapter.py`, local PostgreSQL 16 with TLS) | **1560 passed**, 31 deselected, 0 failed (217 s) on `8efd09d`; **1577 passed**, 31 deselected, 0 failed (269 s) after the pass-2 fixes (§10) |
| `tests/unit/test_m11e2_fargate_dispatcher.py` (mocked ECS/SQS, task entry point) | 57 passed |
| `tests/unit/test_m11e2_deploy_scripts.py` (stub aws/sam/docker) | 38 passed |
| `tests/unit/test_m11e1_aws_templates.py` (policy mutation tests) | 32 passed |
| `cfn-lint` 1.57.1 on `template.yaml`, `ecr.yaml` | clean |
| `sam validate --lint` (placeholder credentials, no AWS call) | valid |
| `scripts/verify_aws_templates.py` and `--lambda-memory-limit` | OK |
| `bash -n` on every deploy script; account-id/DSN/secret grep over `deploy/aws` | clean |
| Frontend | untouched; not re-run |
| CI on the final head | see the PR checks (recorded in the PR, not here) |

## 6. Cost

Estimates and assumptions are in the runbook §15. In short: ≈ USD 0.004 per Fargate minute for 4 vCPU / 8 GB, so a
12-minute invoice ≈ USD 0.05 and ~400 worst-case invoices a month would approach the USD 20 budget on their own. Idle cost
is unchanged (ECR storage, alarms); the VPC, subnets, internet gateway, security group and cluster are free. The dispatcher
Lambda bills ≈ 12 minutes × 0.25 GB ≈ 180 GB-s per job while it waits. No new budget was created.

## 7. Deviations and known limits (not hidden)

* **D-1 Lambda worker removed.** The worker is no longer a Lambda function; `WorkerFunction`, `WorkerMemoryMb`,
  `WorkerEphemeralStorageMb`, `WorkerErrorsAlarm`, `WorkerDurationAlarm`, `WorkerFunctionArn` and the Fargate fallback template
  were removed. The M11E.1 report is annotated, not rewritten.
* **D-2 Time budget.** The dispatcher's 900 s Lambda timeout bounds image pull + start-up + OCR (default hard deadline
  720 s inside the task). The 12-minute benchmark gate remains; inputs needing longer fail with exit 8 / `DISPATCHER_DEADLINE`
  and end in the DLQ rather than silently running forever. The multi-GB image pull time on Fargate is **unmeasured**.
* **D-3 Failed-before-claim jobs stay `QUEUED`.** If all three deliveries fail before the job is claimed (e.g. a bad image),
  the message reaches the DLQ and the job remains `QUEUED`; re-submitting re-dispatches it (documented, pre-existing semantics).
* **D-4 Head-of-line blocking.** One message group means one stuck message delays later jobs; mitigated by the visibility
  back-off (60/300/900 s), not eliminated.
* **D-5 Public IPv4.** Without NAT or endpoints the task needs a public IP to reach ECR, S3, SSM and Neon (outbound only, no
  inbound rule). A private-subnet design would need a NAT gateway or interface endpoints (monthly cost) and was rejected.
* **D-6 Writable root filesystem.** The task definition does not set `ReadonlyRootFilesystem` (the benchmark ran with a
  read-only root and a `/tmp` tmpfs; Fargate has no tmpfs equivalent for the image's `HOME=/tmp`). Unverified on AWS.

## 8. Unverified (needs a real deployment)

Everything that touches AWS: CloudFormation accepting the new resources (VPC/ECS/IAM), `sam deploy --image-repositories`
with a parameterised `ImageUri`, ECS `RunTask` permissions and the `iam:PassRole` condition, the EntryPoint/Command
override against the image's `awslambdaric` entry point, public-IP reachability of ECR/S3/SSM/Neon from the task, Fargate
image-pull time, the `AWS_REGION` environment inside the task, dispatcher timing against the 900 s limit, the SQS event-source
mapping with FIFO, the Fargate vCPU quota, SigV4 on the Function URL, real Clerk/Neon behaviour and the authenticated
acceptance list (runbook §8). Passing CI proves the template is well-formed and policy-compliant, not that it deploys.

## 9. Real AWS finding: the migration SQL files were missing from the installed package

After this milestone's correction a **real pass 1 reached `CREATE_COMPLETE`** (the Fargate/dispatcher design deployed). The
first migration invocation then failed **before any database access**:

```
FileNotFoundError: /opt/venv/lib/python3.11/site-packages/ap_agent/db/migrations/0001_memory_schema_bootstrap.sql
```

* **Cause.** `migration_runner.MIGRATIONS_DIRECTORY` is `Path(__file__).parent / "migrations"`, but `db/migrations/` is a plain
  directory (no `__init__.py`) and `pyproject.toml` did not list the `.sql` files as package data, so the built wheel — hence the
  container image, which installs the project into `/opt/venv` — contained **zero** SQL files (verified: the wheel built from the
  previous head has none). Local pytest (`pythonpath = ["src"]`) and editable installs read the source tree and masked it; the
  image smoke test only imported the handler and never called `load_all_migrations()`.
* **No database mutation occurred.** The loader fails before `open_connection`; the migration DSN was never read and no
  statement ran. Nothing was applied, so no `schema_migrations` row exists yet.
* **Fix (nothing else changed).** `[tool.setuptools.package-data] ap_agent = ["db/migrations/*.sql"]` in `pyproject.toml`. No SQL
  content, migration ID, expected checksum or execution behaviour was touched; the Fargate worker, API, frontend and schema are
  unchanged.
* **Regression tests.** `tests/unit/test_m11e2_migration_packaging.py` builds the wheel, asserts it contains exactly the five
  manifest-referenced files byte-for-byte, installs it into an isolated directory and runs
  `scripts/check_installed_migrations.py` against the *installed* copy (five migrations, IDs in manifest order, every checksum equal
  to the immutable manifest); mutation tests remove each file in turn (and alter one) and require the check to fail. The CI
  `images (migrate)` job runs the same script **inside the built image** against `/opt/venv/.../site-packages`, including the
  per-file removal mutation.
* **Redeploy** (CloudShell, from the repository root, same shell variables as the first deployment): rebuild only the migration
  image, update the existing pass-1 stack, then migrate:

```bash
cd ~/accounts-payable-agent && git pull origin claude/great-bardeen-w8wwlz && cd deploy/aws/scripts
export AWS_REGION=eu-west-2 CLERK_PUBLISHABLE_KEY='pk_...' CLERK_ISSUER='https://<instance>.clerk.accounts.dev'
./build-and-push.sh migrate && ./preflight.sh && ./deploy.sh pass1 && ./invoke-migration.sh migrate
```

  `build-and-push.sh migrate` records a new image digest and keeps the other recorded URIs; `deploy.sh pass1` updates the existing
  stack in place (the dispatcher Lambda shares this image, so it is refreshed too) and still passes no `FrontendOrigin`. Continue
  with the identity mapping (runbook §7), `deploy.sh pass2` and the smoke tests.

## 10. Real AWS findings from pass 2 (three live defects)

Pass 2 deployed; the authenticated frontend recognised the Clerk user and organisation but showed *Backend unavailable*, the API log
group stayed empty, and two helper scripts misbehaved. All three are fixed here; nothing else changed.

1. **API Function URL permission (the cause of *Backend unavailable*).** Since October 2025 a Function URL call needs *both*
   `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction`; `WebRole` held only the first, so the SigV4 request was refused before the
   API ran. Fix: a second, separate `WebRole` statement `InvokeApiFunctionViaUrl` — `lambda:InvokeFunction` on exactly
   `!GetAtt ApiFunction.Arn`, only when `Bool lambda:InvokedViaFunctionUrl = "true"`. The existing `InvokeFunctionUrl` statement
   (`lambda:FunctionUrlAuthType = AWS_IAM`) is unchanged; no resource, principal or action was broadened. The policy verifier and tests
   fail if either action, either condition or the single-resource scoping disappears, if a second `InvokeFunction` statement is added,
   or if any other role gains `lambda:InvokeFunction`.
2. **`invoke-migration.sh identity …`.** `jq -cn '…' --args "$@"` let `--tenant-key` be parsed as a jq option. The payload is now
   built as `jq -cn --args '{action:"identity",args:$ARGS.positional}' -- "$@"`. Tests (stub `aws`) assert exact argument order and
   verbatim delivery of `--tenant-key/--display-name`, `register` with UUID/org/user/role, values with spaces, jq-option look-alikes
   (`--help`, `-n`, `--args`, a literal `--`) and shell metacharacters — nothing is evaluated by a shell and no secret is involved.
3. **`smoke.sh` forged-header status.** Production is correct: the inbound `x-ap-agent-clerk-authorization` header is stripped and,
   with no real Clerk session, the route policy answers **401 `AUTHENTICATION_REQUIRED`** (403 `ORGANIZATION_REQUIRED` is for a
   signed-in user without an active organisation; `frontend/src/lib/auth/route-access.ts`). `smoke.sh` now sends the forged header,
   expects 401 and asserts the safe body contains `AUTHENTICATION_REQUIRED`.

**Redeploy** (CloudShell; the image does not change, only the template and scripts):

```bash
cd ~/accounts-payable-agent && git pull origin claude/great-bardeen-w8wwlz && cd deploy/aws/scripts
export AWS_REGION=eu-west-2 CLERK_PUBLISHABLE_KEY='pk_...' CLERK_ISSUER='https://<instance>.clerk.accounts.dev'
./preflight.sh && ./deploy.sh pass2 && ./smoke.sh
```

IAM changes propagate within a minute or so; then reload the frontend. The still-unverified part is that the real Function URL now
accepts the call (only a live request can show it).

## 11. Real AWS finding: a Neon pooled endpoint rejects startup options (M11E.3)

With the frontend/API permissions fixed, a real request failed inside the API:

```
psycopg.OperationalError: ERROR: unsupported startup parameter in options: statement_timeout.
Please use unpooled connection or remove this parameter from the startup package.
```

The runtime DSN is correct (least-privilege runtime role, the **pooled** endpoint — required by the transport policy).

* **Cause.** `ap_agent.db.connection.open_connection` (and `create_connection_pool`) always passed
  `options="-c statement_timeout=… -c lock_timeout=…"` to `psycopg.connect`. A transaction-pooled endpoint (PgBouncer) refuses
  every startup option it does not track, so *every* database call failed — the readiness probe, Clerk identity resolution, the
  dashboard, the operations API and the Fargate worker all go through `open_connection`. Local/CI databases accept startup
  options, so nothing caught it.
* **Fix (`src/ap_agent/db/connection.py`, nothing else in the application).**
  * Pooled endpoint (host contains `-pooler`): **no startup options**. Right after connecting, `apply_transaction_timeouts` runs
    parameterised `set_config('statement_timeout', %s, TRUE)` and `set_config('lock_timeout', %s, TRUE)` (the transaction-local
    form, like `set_tenant_context`) and **reads them back from `pg_settings`**; any mismatch raises
    `PostgresConfigurationError` and the connection is never yielded. They end with the transaction, so they cannot reach
    another transaction, request or tenant through the pooler. Session-level `SET` is never used.
  * Direct endpoint: unchanged — the same startup options, nothing extra executed (the owner/migration DSN is direct).
  * The transport policy is unchanged: the runtime DSN must still be the pooled endpoint; production traffic was **not** moved
    to the direct DSN.
  * `create_connection_pool` now **fails closed** on a pooled endpoint (a client-side pool reuses a connection across many
    transactions, which transaction-local limits cannot cover). Nothing consumes that pool and the AWS deployment already sets
    `AP_AGENT_API_DISABLE_CONNECTION_POOL=true`, so behaviour is unchanged there.
  * Invariant relied upon (and enforced by a test scanning `src/`): every database call is one `open_connection` = one
    transaction; nothing commits, rolls back, switches to autocommit or connects around `open_connection` mid-connection.
* **Tests.** `tests/unit/test_m11e3_pooled_connection.py` emulates the pooler's rejection (the pre-fix implementation, copied
  into the test, raises exactly the observed error; the fixed one connects), and pins: no startup options when pooled, the
  unchanged options when direct, transaction-local parameterised verified limits, no leakage between transactions, rollback
  discards them, unverified limits fail closed, the transport policy and the pool refusal. `tests/api/test_m11e3_pooled_postgres.py`
  forces the pooled path against real PostgreSQL through the runtime role and proves: both limits are active and the statement
  timeout really fires; after COMMIT/ROLLBACK and on a fresh connection the server defaults are back; tenant context and RLS
  (own-tenant visibility, cross-tenant write refused) are unchanged; and that Clerk identity resolution, the dashboard/review
  repository, the operations API paths, the Fargate worker paths (`peek_job`, `claim_job`), the readiness probe and the
  migration runner all work on it, each with no startup options.
* **New script.** `deploy/aws/scripts/record-image.sh` records an image built elsewhere (CodeBuild) in `images.env` by digest,
  verifying it exists in ECR and leaving the other entries untouched (tested with stub CLIs).

**Redeploy** (only `api`, `migrate` — which the dispatcher shares — and `worker` contain `src/ap_agent`; `web` does not):

```bash
# 0. update the checkout and set the usual variables (no secrets are typed here)
cd ~/accounts-payable-agent && git pull origin claude/great-bardeen-w8wwlz && cd deploy/aws/scripts
export AWS_REGION=eu-west-2 CLERK_PUBLISHABLE_KEY='pk_...' CLERK_ISSUER='https://<instance>.clerk.accounts.dev'
TAG="$(git rev-parse --short=12 HEAD)"; FULL="$(git rev-parse HEAD)"; export IMAGE_TAG="$TAG"

# 1. the small images that contain src/ap_agent (the dispatcher shares the migrate image). web is a Next.js image: unchanged.
./build-and-push.sh api migrate          # records both BY DIGEST in .local/images.env; the other recorded URIs are kept

# 2. the large worker image: build it in AWS CodeBuild (CloudShell's disk is too small). Use your existing worker build project
#    (it must build docker/worker.lambda.Dockerfile at this commit and push ap-agent-production/worker:$TAG).
aws codebuild start-build --project-name <your-worker-build-project> --source-version "$FULL" \
  --environment-variables-override name=IMAGE_TAG,value="$TAG",type=PLAINTEXT --query 'build.id' --output text
#    wait until it reports SUCCEEDED (replace <build-id> with the id printed above):
until [ "$(aws codebuild batch-get-builds --ids <build-id> --query 'builds[0].buildStatus' --output text)" != IN_PROGRESS ]; do sleep 20; done
aws codebuild batch-get-builds --ids <build-id> --query 'builds[0].buildStatus' --output text      # must print SUCCEEDED

# 3. record the worker image by digest, safely (verifies it exists in ECR, replaces only WORKER_IMAGE_URI, keeps images.env.bak)
./record-image.sh worker "$TAG"
cat ../.local/images.env | sed -E 's#^([A-Z]+_IMAGE_URI=)[^@:]*(/[^@]*)@(sha256:.{12}).*#\1<registry>\2@\3...#'   # shows names + digests only

# 4. redeploy pass 2 (updates the API, dispatcher/migrate functions and the OCR task definition; no FrontendOrigin change)
./preflight.sh && ./deploy.sh pass2
```

Verify (the frontend URL is `aws cloudformation describe-stacks --stack-name ap-agent-production --query "Stacks[0].Outputs[?OutputKey=='FrontendUrl'].OutputValue" --output text`):

1. **Health through the frontend.** Signed in to the frontend in your browser, open `<FrontendUrl>api/backend/health`. Expected: JSON with `"status":"SUCCEEDED"`. (Anonymous requests correctly get 401.)
2. **Dashboard.** In the same session open `<FrontendUrl>api/backend/api/v1/dashboard`. Expected: JSON with `"status":"SUCCEEDED"` (an empty dashboard for a new tenant is fine) — not *Backend unavailable*.
3. **No pooler error.** `aws logs tail /aws/lambda/ap-agent-production-api --since 15m | grep -c "unsupported startup parameter"` must print `0`, and the log should show the requests.
4. **Smoke.** `./smoke.sh` — every line `ok`.
5. **One real invoice end to end.** Upload one invoice on *Operations*; then `./observe-queue.sh` until a task shows `exit: 0` (one RUNNING task, then a STOPPED task with exit 0), `aws logs tail /ecs/ap-agent-production-ocr --since 30m` shows `worker task: PROCESSED.`, the queue and DLQ are empty, and the job ends *completed* or *review required* in the interface (nothing about `statement_timeout`, `unsupported startup parameter` or `WORKER_INTERRUPTED`). Work it through review as in §8.

Unverified until you run it: that the real pooler accepts the transaction-local `set_config` calls and the `pg_settings`
read-back (both are ordinary statements PgBouncer forwards), and the end-to-end behaviour above.

## 12. Real hosted finding: a missing header could not be supplied; confidence rendered as 9999% (M11E.4)

The first real invoice exposed two production defects.

1. **`SUPPLIER_NAME_MISSING` could not be corrected.** The agent raised the reason, but *Supplier name* was absent from the *Field
   to correct* selector. Three rules refused a header that did not already exist in the stored record:
   `build_command_capabilities` listed a correctable header only if it was present in `header_field_values`,
   `validate_review_command` answered `HEADER_FIELD_NOT_PRESENT`, and `apply_correction_overlay` rejected an absent header target
   (`CONFIRM_SUPPLIER` is deferred and no alternative). A genuinely missing value was therefore impossible to supply.
2. **Confidence 9999% / 10000%.** The backend sends confidence on a 0–100 scale (the normaliser and the human-review overlay use
   100); `formatConfidence` treated it as a 0–1 proportion and multiplied by 100.

**Fix**

* **Insertion, only when explicitly allowed.** A header field in `InterfaceConfig.correctable_header_fields` is now offered in
  `correction_policy.header_fields` even when absent, with `current_value: null`. `validate_review_command` accepts a correction
  for it only with `previous_value = null` (a non-null, i.e. forged, previous value is `PREVIOUS_VALUE_MISMATCH`); present fields
  still need the exact stored value. Arbitrary fields (`CORRECTION_FIELD_NOT_ALLOWED`), a line number on a header, duplicate
  targets, stale revisions, unknown evidence, blank values and missing reason/evidence are rejected exactly as before.
  `HEADER_FIELD_NOT_PRESENT` is no longer produced.
* **Overlay.** `apply_correction_overlay` creates the missing header in the *derived* normalization record: appended after the
  existing fields (append-only), canonical value type for the field (`header_value_type`: text, date, decimal or currency code —
  a test asserts it equals the normaliser's type for every header field), `HUMAN_REVIEW_CORRECTION` provenance
  (`previous_value=<missing>`, decision id, reason, evidence), confidence `100`, `review_required=false`. A non-null
  `previous_value` for an absent field raises `OVERLAY_PREVIOUS_VALUE_MISMATCH`. The input record and the stored
  `invoice_memory_records` row are never touched; the result goes into the new derived version, from which the resumed stages
  (`REFERENCE_MATCHING` for a supplier) read `SUPPLIER_NAME`.
* **Frontend.** `formatConfidence` honours the 0–100 contract: `99.99 → 99.99%`, `100 → 100%` (at most two decimals), `null`
  stays *unavailable*, low confidence is below 70. The correction editor already sent `previous_value: null` for a null current
  value; it now simply receives the target.
* **Existing live case 36258** becomes correctable after deployment **without a new upload**: it is a stored case, and the
  selector, validation and overlay all work from the stored record (the API and worker images must be redeployed).

**Behaviour change to note:** the selector now offers every *configured* header field, not only the extracted ones (the default
configuration lists thirteen). That is what "explicitly included in `correctable_header_fields`" means; to narrow it, change the
configuration, not the code.

**Tests** (they fail on the previous code): `tests/unit/test_m11e4_missing_header_correction.py` (offered target, null previous,
unconfigured and forged cases, line/duplicate/stale/evidence rules, overlay insertion, types, provenance, immutability,
hydration round-trip), `tests/api/test_m11e4_missing_supplier_postgres.py` (real PostgreSQL, real API and worker: the complete
claim → correct missing supplier → resume flow, restart at `REFERENCE_MATCHING`, supplier matched downstream, derived version
holds the supplier, original memory unchanged, rejections without mutation) and the frontend formatter, detail-section and
correction-editor tests. Three existing assertions were updated to the new contract (the capability listing and two
"field not present" rejections).

**Redeploy** (only the Python images contain this change — `api`, `migrate`/dispatcher and `worker`; the frontend image has the
formatter fix, so `web` is rebuilt too):

```bash
cd ~/accounts-payable-agent && git pull origin claude/great-bardeen-w8wwlz && cd deploy/aws/scripts
export AWS_REGION=eu-west-2 CLERK_PUBLISHABLE_KEY='pk_...' CLERK_ISSUER='https://<instance>.clerk.accounts.dev'
TAG="$(git rev-parse --short=12 HEAD)"; FULL="$(git rev-parse HEAD)"; export IMAGE_TAG="$TAG"
./build-and-push.sh api web                     # api: command validation/overlay; web: confidence formatter
# worker (large) via CodeBuild, then recorded by digest:
BUILD_ID=$(aws codebuild start-build --project-name <your-worker-build-project> --source-version "$FULL" \
  --environment-variables-override name=IMAGE_TAG,value="$TAG",type=PLAINTEXT --query 'build.id' --output text)
until [ "$(aws codebuild batch-get-builds --ids "$BUILD_ID" --query 'builds[0].buildStatus' --output text)" != IN_PROGRESS ]; do sleep 20; done
aws codebuild batch-get-builds --ids "$BUILD_ID" --query 'builds[0].buildStatus' --output text   # SUCCEEDED
./record-image.sh worker "$TAG"
./build-and-push.sh migrate                     # the dispatcher shares it; keeps the code identical across images
./preflight.sh && ./deploy.sh pass2 && ./smoke.sh
```

Then, signed in, open case 36258: *Field to correct* now lists *Supplier Name (header)*; claim the case, correct it (evidence is
required), request resume, and confirm the job restarts at reference matching and the supplier is matched. Confidence values
read `99.99%` / `100%`.

## 13. Is it safe to attempt another real pass 1?

Yes, subject to `./preflight.sh` printing "Preflight passed" (no `BLOCK` line) — in particular the Fargate quota and the
`ROLLBACK_COMPLETE` check — and the stack-only cleanup of the failed stack first. The cheapest rollback is
`./teardown.sh --stack-only`. Expect further first-contact issues listed in §8; each is diagnosable with
`./observe-queue.sh` and the runbook §10 table.

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
| Full backend suite (`pytest -m "not requires_paddle" --ignore=tests/unit/test_paddleocr_adapter.py`, local PostgreSQL 16 with TLS) | **1549 passed**, 31 deselected, 0 failed (244 s) |
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

## 10. Is it safe to attempt another real pass 1?

Yes, subject to `./preflight.sh` printing "Preflight passed" (no `BLOCK` line) — in particular the Fargate quota and the
`ROLLBACK_COMPLETE` check — and the stack-only cleanup of the failed stack first. The cheapest rollback is
`./teardown.sh --stack-only`. Expect further first-contact issues listed in §8; each is diagnosable with
`./observe-queue.sh` and the runbook §10 table.

# M11D Core — Operations Console and Controlled Workflow Execution

Status: **PR open, not merged. Not merge-ready until the remote PostgreSQL CI path is green.**
Local results below are from **local PostgreSQL 16** (`ap_agent_m8_test`), *not* the Neon
database used by the repository's CI secret. Remote CI is authoritative.

Starting commit: `b3c0029` (M11C, merged). Branch: `claude/nice-faraday-m46tqu` (the branch
assigned by the execution harness, not `claude/m11d-operations-console`).

## 1. Scope

Delivered: a vertical slice. A user opens Operations, uploads an invoice, it is enqueued
(no OCR in the request), a separate worker runs the existing Phase 1–8 pipeline, the job ends
*completed* or *review-required*, the reviewer claims and approves/corrects the case (M11C),
requests resume, and the worker executes from the plan's restart stage.

**Single worker only.** Explicitly *not* implemented (deferred to M11E): multiple-worker claiming
beyond `FOR UPDATE SKIP LOCKED` safety, leases, heartbeats, crash recovery, dead-letter queue.

Deferred to M11E: real authentication (OIDC/JWT), cloud object storage for uploads, multiple
workers, leases and crash recovery, repeated review cycles hardening beyond the tested second
cycle, hosted deployment, production monitoring.

## 2. Architecture and trust boundaries

`browser → Next.js (same-origin, CSRF token, multipart-only, byte cap, field allow-list, identity
built server-side) → FastAPI (role policy, size middleware, signature validation) → PostgreSQL
queue ← worker process`. Dependency direction is unchanged (`orchestration → tools → adapters →
artifacts → models`). No config instance is a module global; heavy imports stay lazy.

Upload control plane: server-generated `artifact://<tenant>/<job>/<name>` paths under
`AP_AGENT_ARTIFACT_ROOT`, extension + media-type + magic-byte validation, 10 MiB cap, 128-char
name, traversal/symlink refusal, SHA-256 hashing; no path or content is ever exposed to the
browser. Payment- or identity-shaped form fields are rejected. Cloud object storage is M11E.

## 3. Schema (migration `0004_durable_operations.sql`)

- `workflow_jobs`: `QUEUED→RUNNING→SUCCEEDED|REVIEW_REQUIRED|FAILED`, guard trigger
  (immutable identity, `lock_version+1`, terminal is final, no delete), unique
  `(tenant, job_type, idempotency_key)` and `(tenant, resume_plan_id)`, RLS.
- `workflow_job_events`: append-only.
- `invoice_memory_versions`: append-only derived versions, unique `(tenant, resume_plan_id)`.
  The original `invoice_memory_records` is never updated.
- `review_cases`: immutable `memory_version_id`, `source_resume_plan_id`.
- Views `review_case_effective_memory`, `workflow_effective_memory` (`security_invoker`).
- No first-class resume-plan table: M11C's audit payload is used (gained `resume_job_id`,
  `source_memory_record_id`, `source_memory_version_id`).

## 4. API and frontend

`POST /api/v1/operations/submissions` (202 new / 200 idempotent replay / 409 conflict;
AP_OPERATOR and TENANT_ADMIN only; no client identity fields), `GET /api/v1/operations/jobs`,
`GET /api/v1/operations/jobs/{job_id}` (all four roles). Frontend: `/operations`,
`/operations/jobs/[jobId]`, resume-job panel on the review page (refreshes the workflow status when
the resume job settles), manual refresh plus bounded polling, desktop and mobile layouts.

## 5. Resume semantics

The resume job is inserted inside the M11C resume transaction (deterministic id
`uuid5(tenant, plan_id)`). The executor fails closed, with stable codes and before any stage runs,
unless the original/effective memory hash, normalization hash, overlay hash and decision evidence,
derived version, plan identity, restart stage, source document hash and upload bytes all verify.
It then rehydrates upstream state, applies the correction overlay to a deep copy, and runs only the
restart stage and later (`FINANCIAL_VALIDATION` for financial, `REFERENCE_MATCHING` for supplier
corrections, `MEMORY_PERSISTENCE` for an approval). The derived version is appended once
(`ON CONFLICT DO NOTHING`). If validation still needs review, a new linked OPEN review case is
created (fail closed into visible `REVIEW_REQUIRED`); otherwise the workflow is `COMPLETED`.

Roles: submit = AP_OPERATOR + TENANT_ADMIN; read = all four; resume keeps M11C's reviewer/admin policy.

## 6. Verification (local PostgreSQL 16, not Neon)

- Backend: `1194 passed, 29 skipped, 3 failed`. The 3 failures are in
  `tests/unit/test_paddleocr_adapter.py` (PaddleOCR is not installed in this sandbox) and fail
  identically on the pre-M11D base; the PaddleOCR-requiring M11D test is `requires_paddle` and is
  run by the m8 workflow. M11D additions: 24 + 11 + 7 real-PG tests, 83 unit tests.
- Frontend: lint, `tsc`, 460 Vitest tests, production build, `check:build-secrets` clean.
- `npm run test:e2e:real-operations`: 9 passed, 1 skipped (automatic completion needs a real
  PaddleOCR read), then 21/21 direct PostgreSQL checks (original memory unchanged; resume ran only
  `MEMORY_PERSISTENCE`; one derived version; events/jobs/versions/memory refuse UPDATE/DELETE;
  another tenant sees nothing; no job visible without tenant/worker scope).
- Accessibility (axe, WCAG 2 A/AA + 2.2 AA): no serious/critical violations at 1280 and 390 px.
- `npm run demo:m11d -- --check` passes.
- **OCR provider actually exercised locally:** real Tesseract via the Phase 3 fallback router
  (always routes to review by design). Real PaddleOCR is exercised only by CI
  (`m8-postgres-acceptance`, enabled for this branch) and the `ocrProvider=paddleocr` test.

## 7. Deviations from the notebook/M11C (documented, not silent)

1. Effective-memory views so review reads use the derived version for a second-cycle case.
2. Worker-scope RLS (`ap_agent.worker_scope='queue'`) on `workflow_jobs` only — an
   application-level GUC like the tenant GUC.
3. The worker settles workflow state (`HUMAN_REVIEW`/`REVIEW_REQUIRED` or `COMPLETED`/`SUCCEEDED`)
   because M8 persists the last stage while M11C's resume guard requires `HUMAN_REVIEW`.
4. Workflow `source_name` is set to the validated upload name (the append-only memory record keeps
   `original.<ext>`).
5. Engine: `_run_stage_loop` extracted; `resume_invoice_workflow` added. Behaviour of the existing
   entry point is unchanged.
6. Additive audit payload fields (see §3).
7. M11C wording/tests unchanged: banner/panel text is conditional on `operationsEnabled`.

## 8. Limitations

A killed worker leaves its job `RUNNING`; a failed resume leaves the workflow `IN_PROGRESS` at the
restart stage; artifacts live on a local shared filesystem; prototype header authentication — not
for a public write-enabled deployment; no payment/ERP execution; M11C handoffs made before
migration 0004 have no job; a file chosen before React hydrates is dropped (tests re-select).

## 9. Screenshots (`docs/m11d-screenshots/`)

Essential five: `01-operations-upload`, `02-running-pipeline`, `03-review-required`,
`04-review-action-and-resume`, `05-completed`. Extra mobile views: `06`, `07`.

## 10. Running it

```bash
cd frontend && npm run build
AP_AGENT_TEST_POSTGRES_DSN=<ap_agent_m8_test DSN> npm run demo:m11d
```

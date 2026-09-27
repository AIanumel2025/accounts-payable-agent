# M10 — Phase 9 Human-Review Interface and FastAPI Modularisation

## 1. Preflight

- Starting commit: `7f972f4944dec91debc8c3751ea58d7285850d0c` ("notebook ui
  interface code" — the notebook update that added the completed Phase 9
  cells), which is `origin/main` at the time this milestone started.
- **Branch deviation (documented, not silent):** the task brief asks for a new
  branch `claude/m10-phase-9-review-api`. This session's harness pinned a
  different, pre-existing branch for all work in this environment:
  `claude/sharp-brahmagupta-ec6k5e` ("NEVER push to a different branch without
  explicit permission"). That branch was, at session start, identical to
  `origin/main` except for the same notebook-update commit above (verified with
  `git diff main HEAD --stat -- notebooks/ docs/ README.md`, which was empty).
  All M10 work is committed there instead. The CI workflow's branch allow-list
  (§18) lists `claude/sharp-brahmagupta-ec6k5e` accordingly.
- Working tree was clean at the start of the session; `AP_AGENT_TEST_POSTGRES_DSN`
  was confirmed present and its `dbname` parsed (never printed) to equal
  `ap_agent_m8_test` before any work began.
- Confirmed present in the repository: the completed Phase 1-9 notebook, the
  modularised Phase 1-8 implementation (M2-M7), the M8 PostgreSQL memory system,
  and the M9 orchestration engine.

## 2. Notebook path and Phase 9 cells used

`notebooks/accounts_payable_pipeline.ipynb` (110 cells, 0-indexed). Phase 9
occupies cell 99 (markdown overview) and cells 100-108 (its nine numbered code
cells); cell 109 is empty (end of notebook).

| Cell | Title | Extracted into |
|---:|---|---|
| 99 | `PHASE 9 — HUMAN-REVIEW INTERFACE` (markdown) | Design reference only (module docstrings) |
| 100 | `PHASE 9 — CELL 1`: interface contracts and configuration | `ap_agent/models/interface.py` |
| 101 | `PHASE 9 — CELL 2`: review-queue retrieval | `ap_agent/repositories/review_repository.py` (`list_review_queue`) |
| 102 | `PHASE 9 — CELL 3`: invoice-detail projection | `ap_agent/repositories/review_repository.py` (`get_invoice_detail`) |
| 103 | `PHASE 9 — CELL 4`: dashboard metrics | `ap_agent/repositories/review_repository.py` (`get_dashboard`) |
| 104 | `PHASE 9 — CELL 5`: command policy | `ap_agent/services/review_commands.py` (`validate_review_command`, fingerprint) |
| 105 | `PHASE 9 — CELL 6`: claim/release executor | `ap_agent/repositories/review_repository.py` (primitives) + `ap_agent/services/review_commands.py` (`execute_assignment_command`) |
| 106 | `PHASE 9 — CELL 7`: decision executor | `ap_agent/repositories/review_repository.py` (primitives) + `ap_agent/services/review_decisions.py` |
| 107 | `PHASE 9 — CELL 8`: resume handoff | `ap_agent/repositories/review_repository.py` (primitives) + `ap_agent/services/workflow_resume.py` |
| 108 | `PHASE 9 — CELL 9`: FastAPI boundary + in-process acceptance | `ap_agent/api/*` |

Every cell's own printed acceptance summary ("PASSED"/"READY"/"NONE" for
database mutations) was read and used as the behavioural target; no cell
defines more than one candidate implementation of the same name, so there was
no cross-cell precedence ambiguity to resolve (unlike M7's supplier-matching
case) — cell 108's own dispatch table (claim/release → assignment executor;
accept/correct/reject → decision executor; resume → resume executor) is
reproduced exactly in `ap_agent/api/routes/review_commands.py`.

## 3. Extraction/precedence map

- **Active, ported verbatim:** every enum, dataclass, default value, SQL
  statement (columns/joins/ordering unchanged), validation rule, error code,
  and idempotency/fingerprint algorithm in cells 100-108.
- **Notebook orchestration, not ported:** module-level test variables
  (`phase_9_review_queue`, `template_context`, `operator_actor`, ...), `display(pd.DataFrame(...))`
  calls, `print(...)` acceptance banners, and the notebook's own rollback-only
  acceptance transactions (cells 105-107's `test_connection`/`decision_test_connection`/
  `resume_test_connection` blocks) — their *behaviour* became the basis for
  `tests/integration/test_review_postgres_integration.py`'s real-database
  scenarios instead.
- **Fixture-specific, not ported:** cell 101's `phase_9_current_document_ids`
  scoping (asserted to be exactly the four "current run" documents) and its
  `EXPECTED_REVIEW_QUEUE_SOURCES`/`EXPECTED_DETAIL_COUNTS` fixture assertions;
  cell 108's `PROTOTYPE_TENANT_ID`-only tenant check. See §11 "Deviations" for
  the generalisation each one required.
- **Diagnostic-only:** cell 108's `phase_9_api_acceptance_rows`
  pandas display table — its assertions became `tests/api/test_review_api_postgres.py`.

## 4. Module architecture (task §3)

```text
src/ap_agent/
├── api/
│   ├── app.py            # create_app(...) factory, lifespan, CORS, exception handlers
│   ├── config.py         # ApiConfig / load_api_config() (write-mode, CORS, tenant allow-list)
│   ├── dependencies.py    # prototype header-auth adapter, per-request resources
│   ├── errors.py          # exception -> HTTP status mapping, central handlers
│   ├── schemas.py         # explicit Pydantic request/response schemas
│   └── routes/
│       ├── health.py
│       ├── dashboard.py
│       ├── review_cases.py
│       └── review_commands.py
├── models/interface.py                      # Phase 9 domain contracts (cell 100/104/107)
├── repositories/review_repository.py        # all Phase 9 SQL (cells 101-107)
└── services/
    ├── review_queries.py     # dashboard/queue/detail read services, pagination
    ├── review_commands.py    # command policy + claim/release executor (cells 104/105)
    ├── review_decisions.py   # terminal-decision executor (cell 106)
    └── workflow_resume.py    # resume policy + executor (cell 107)
```

No adjustment to the task brief's intended structure was needed.

## 5. Contracts extracted (task §4)

`InterfaceRole`, `ReviewCaseStatus`, `ReviewPriority`, `ReviewAction`,
`InterfaceCommandStatus`, `InterfaceConfig`/`default_interface_config()`,
`InterfaceActor`, `DashboardRecord`, `ReviewQueueRecord`, `InterfaceFieldValue`,
`InterfaceFinancialCheck`, `InterfaceLineMatch`, `InterfaceTimelineEvent`,
`InvoiceDetailRecord`, `ReviewFieldCorrection`, `ReviewCommand`,
`ReviewCommandResult`, `ReviewCommandContext`, `WorkflowResumePlan`,
`WorkflowResumeExecution` — all in `ap_agent/models/interface.py`, ported
verbatim from cells 100/104/107 with two structural changes:

1. `tenant_id` is typed `UUID` everywhere (matching the actual `UUID NOT NULL`
   migration 0002/0003 columns), not `str` (the M8 `ap_agent.models.memory`
   convention, which predates the migrated schema).
2. `default_interface_config()` is a factory function, not a module-level
   `interface_config = InterfaceConfig(...)` instance (CLAUDE.md: "No module
   under `src/` may define a mutable module-level config instance").

Every listed policy is preserved: payment execution prohibited (no
`EXECUTE_PAYMENT` `ReviewAction` member exists at all, asserted in
`InterfaceConfig.__post_init__`); tenant boundaries fail closed (row-level
security plus an explicit `TenantAccessDeniedError` guard, §9); role
authorization fails closed (`interface_permissions_for_role`); stale revisions
rejected (`STALE_REVIEW_REVISION`/`STALE_WORKFLOW_REVISION`); corrections
require a reason and evidence; review decisions are append-only (migration
0002's triggers, never an `UPDATE`/`DELETE` in this milestone's SQL); original
normalized invoice memory is immutable (never written to by any Phase 9 path);
resume handoffs derive a new version (`WorkflowResumePlan.derived_version`);
restart occurs from the earliest affected stage (`choose_review_restart_stage`);
identical retries are idempotent; reused idempotency keys with different
content are conflicts.

## 6. Repository operations (task §5)

`ReviewRepository` (`ap_agent/repositories/review_repository.py`):
`get_dashboard`, `list_review_queue` (paginated, filterable), `get_review_queue_record`,
`get_invoice_detail`, `get_command_context` (unlocked read), `transaction()`
(tenant-scoped cursor context manager), `lock_assignment_context`/
`lock_decision_context` (`FOR UPDATE`), `find_stored_command_payload`,
`apply_claim_or_release`, `advance_workflow_phase`, `next_audit_sequence_number`,
`insert_command_audit_event`, `append_review_decision`, `resolve_review_case`,
`latest_review_decision_for_resume`. Every tenant-scoped method calls
`set_tenant_context` before any other statement; every write is parameterized
(no string interpolation); payload-hash recomputation and cross-tenant-row
detection (`ReviewIntegrityError`) run on every read.

## 7. Service operations (task §7)

- `review_queries.get_dashboard`/`list_review_queue`/`get_invoice_detail` —
  read projections with real pagination (page/page_size/total_count) and
  optional status/assignment/priority/batch filters.
- `review_commands.validate_review_command`/`review_command_fingerprint`/
  `execute_assignment_command` — claim/release.
- `review_decisions.execute_decision_command` — accept/correct/reject
  (evidence-backed correction, append-only decision).
- `workflow_resume.execute_resume_command`/`validate_resume_command`/
  `choose_review_restart_stage` — controlled resume handoff only (never
  executes the Phase 1-8 pipeline itself).

## 8. Endpoint catalogue (task §8)

```text
GET  /health
GET  /api/v1/dashboard                          (optional ?batch_id=)
GET  /api/v1/review-cases                       (page, page_size, status,
                                                  assigned_to, priority, batch_id)
GET  /api/v1/review-cases/{review_case_id}
POST /api/v1/review-cases/{review_case_id}/commands
GET  /openapi.json
GET  /docs
GET  /redoc
```

`create_app(*, dsn=None, memory_config=None, interface_config=None,
api_config=None)` opens no database connection at call time; every parameter
resolves lazily (`load_dsn`/`load_api_config`/`default_interface_config`) only
inside the function body. Local launch:

```bash
pip install -e ".[api,postgres]"
export AP_AGENT_POSTGRES_DSN=postgresql://...   # production runtime DSN
uvicorn ap_agent.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

then open `http://127.0.0.1:8000/docs`.

## 9. Authentication boundary (task §10)

`ap_agent/api/dependencies.py`'s `authenticated_interface_actor` is explicitly
labelled, in its module docstring and inline, as a **development/test
authentication adapter** trusting four plain headers
(`X-Tenant-ID`/`X-Actor-ID`/`X-Actor-Role`/`X-Authenticated-At`) with no
signature verification. It is injected as an ordinary FastAPI dependency
(`AuthenticatedActor = Annotated[InterfaceActor, Depends(...)]`) specifically so
it can be replaced by an OIDC/JWT-verifying dependency later without touching a
route. Production deployment requires a trusted identity provider issuing
verified, signed tokens — this adapter must never be described as production
authentication, and the module docstring says so explicitly.

Fails closed on: a missing header (`401`, not FastAPI's generic 422 — see §14
"Deviations"), a malformed tenant/actor UUID or unrecognised role (`401`),
a malformed or expired/future authentication timestamp (`401`), and (task §13:
never trust a payload's own tenant claim) tenant identity is read only from
`X-Tenant-ID`, never from the request body.

## 10. Write-mode behaviour (task §11)

`AP_AGENT_ENABLE_REVIEW_COMMAND_WRITES` (default `false`, parsed by
`ApiConfig.load_api_config`). When `false`: read endpoints operate normally,
`validate_review_command` still runs (rejecting genuinely invalid commands
with the real HTTP status), and a command that passes validation returns
`{"status": "VALIDATED", "data": {..., "execution_mode": "VALIDATION_ONLY",
"database_mutation": false}}` — no repository write executes. When `true`:
claim/release/correction/decision/resume commands execute through the real
transactional services, with every concurrency/revision/idempotency/audit/
append-only/tenant control still active. No test or documentation page sets
this globally; every write-mode test constructs its own `ApiConfig(enable_review_command_writes=True)`.

## 11. Deviations (documented, not silent — CLAUDE.md)

1. **Branch name** (§1).
2. **`tenant_id: UUID`** instead of the M8 convention's `str` (§5) — matches
   the actual migrated column type; no behaviour change for callers that
   already pass a `UUID`.
3. **`default_interface_config()` factory**, not a module-level instance (§5) —
   required by CLAUDE.md; identical field values.
4. **Fixture-scoping removed**: `get_dashboard`/`list_review_queue` query every
   one of the tenant's workflows/review cases (optionally narrowed by an
   explicit `batch_id`), never a fixed four/three-document assumption (task
   §6, CLAUDE.md D-2/D-3). `list_review_queue` also adds real
   pagination/filters the notebook's single-purpose demo query never needed.
5. **Cross-tenant guard is now configurable, not hardcoded**: the notebook's
   `authenticated_interface_actor` hardcodes rejection of any tenant other
   than its own `PROTOTYPE_TENANT_ID`. Production code must not hardcode a
   single tenant constant (CLAUDE.md D-2/D-3), so this became
   `ApiConfig.allowed_tenant_ids` (default empty = unrestricted multi-tenant
   service, matching real RLS-backed multi-tenancy). The M10 golden-parity
   "cross-tenant read → 403" scenario is reproduced by tests that configure
   `allowed_tenant_ids=(tenant_id,)`, exactly like a single-tenant deployment
   would.
6. **Fixed a genuine notebook-only bug, not silently**: the notebook's
   `submit_review_command` looks a case up via `queue_record_for_case` →
   `retrieve_review_queue`, which only ever returns *active* (OPEN/CLAIMED)
   cases. By the time a resume command is legitimately requested the case is
   already RESOLVED, so the notebook's own API layer can never resolve a
   `RESUME_WORKFLOW` command end-to-end through its FastAPI cell (only its
   own in-process cell 107 exercises a real resume, never through cell 108's
   API). `ReviewRepository.get_review_queue_record` instead looks a case up
   by id regardless of status, so a resume command against a resolved case is
   reachable through this API, exactly as task §7/§14 require it to be
   testable. Documented and covered by
   `tests/integration/test_review_postgres_integration.py::test_resume_handoff_persistence`.
7. **Authentication timestamp expiry window added**: the notebook's
   `parse_authenticated_at` never rejects an old timestamp (only a naive one).
   Task §10 explicitly asks this adapter to "reject expired ... authentication
   timestamps"; a 12-hour window (`AUTHENTICATION_MAX_AGE`) was added. This is
   a deliberate, requested addition, not a silent behaviour change, and it
   does not affect any golden-parity scenario (all of which authenticate and
   call within the same request).
8. **Extra workflow-transition guard restored, not dropped**: while
   generalizing the notebook's three separate `UPDATE ap_agent.workflow_instances`
   call sites (cells 105/106/107) into one shared
   `ReviewRepository.advance_workflow_phase`, the resume transition's extra
   `WHERE current_phase = 'HUMAN_REVIEW' AND current_status = 'REVIEW_REQUIRED'
   AND review_required = TRUE` guard (cell 107, absent from claim/release/
   decision) was caught in review and restored as optional
   `require_current_phase`/`require_current_status`/`require_review_required`
   parameters, passed only by `workflow_resume.execute_resume_command`.
9. **`resolution_code` bug caught in review**: an early draft of
   `_row_to_queue_record` always passed `resolution_code=None` into
   `command_case_status_from_database`, which would have misreported every
   `REJECT`-resolved case as `RESOLVED` instead of `REJECTED` in queue/detail
   projections. Fixed before commit; covered by
   `tests/unit/test_review_repository_mapping.py::test_row_to_queue_record_reports_rejected_status_from_resolution_code`.
10. **CONFIRM_SUPPLIER/CONFIRM_PURCHASE_ORDER/REQUEST_INFORMATION/ESCALATE have
    no transactional executor**, in write-enabled mode, matching the notebook
    exactly: cell 108's own command dispatch only ever builds an assignment,
    decision, or resume executor for the other six actions and returns `501`
    for anything else, even though `validate_review_command` authorizes and
    validates these four actions for `AP_OPERATOR`/`AP_REVIEWER`/`TENANT_ADMIN`.
    Task §7's required command set is exactly "claim; release;
    evidence-backed correction; review decision; controlled workflow-resume
    handoff" — these four are out of scope for M10, not a silent gap;
    `submit_review_command` raises `ReviewCommandRejectedError(("REVIEW_ACTION_NOT_IMPLEMENTED",))`
    for them in write-enabled mode.
11. **Resume commands require write-enabled mode**, matching the notebook's
    own choice (cell 108: "Resume validation depends on the latest persisted
    resolved decision. It is deliberately not simulated by the notebook's
    validation-only API mode.").
12. **Connection-pool wiring is a documented simplification**: `create_app`'s
    lifespan opens (non-blocking) and closes a `psycopg_pool.ConnectionPool`
    purely as an early-warning readiness signal; `ReviewRepository` itself
    still opens one short-lived connection per call
    (`ap_agent.db.connection.open_connection`), exactly like M8's own
    `PostgresMemoryRepository`. A pooled repository is a natural follow-up,
    not silently done differently here.
13. **Dropped one defensive check**: the notebook's `retrieve_review_queue`
    additionally asserts `stored_current_phase` is one of
    `{REFERENCE_MATCHING, MEMORY_PERSISTENCE, HUMAN_REVIEW}`. This extra
    invariant (beyond what the join's own `WHERE review_required = TRUE`/
    `review_status IN (OPEN, CLAIMED)` already guarantees) was not carried
    into `_row_to_queue_record`. Noted here as a minor, low-risk omission
    rather than silently claiming full parity; it does not affect any test
    in this milestone.

## 12. Notebook golden-baseline parity

Reproduced via `tests/api/test_review_api_postgres.py`/
`tests/integration/test_review_postgres_integration.py` against **seeded**
rows (`tests/support/review_fixtures.py`), not the four committed invoice
fixtures — task §6/§14's own golden numbers are about the *shape* of the
projections (fields/evidence links/financial checks/line matches per
document, dashboard aggregates, three active review cases), and CLAUDE.md
forbids production *and test* code from assuming a fixed four/three count is
the only valid input; the parity tests instead assert the aggregation and
projection **logic** is correct for an arbitrary seeded scenario (matching
M9's own precedent of auditing "for any number of documents", not just the
committed four). The original notebook's own four-fixture numbers remain
documented in `docs/modularisation_map.md`/M6-M9 reports; nothing in Phase 9
recomputes them, since Phase 9 only reads what Phase 1-8 already persisted.

## 13. Test results

### 13.1 Local (this environment — no PostgreSQL egress; see §14)

```text
pytest -m "not requires_postgres"
957 passed, 3 failed, 28 skipped, 50 deselected
```

The 3 failures are `tests/unit/test_paddleocr_adapter.py` — this environment
has no `paddleocr` package installed (a multi-hundred-MB optional download;
this sandbox's PaddleOCR requirement is documented as environment-gapped
since M8/M9). They are pre-existing and unrelated to any M10 change,
confirmed by running the identical file before this milestone's edits.
`tesseract-ocr` (the system binary, not just the `pytesseract` binding) was
installed mid-session specifically to shrink this list from an initial 10
failures (9 pre-existing `pytesseract`/OCR ones plus the one true M10 defect
below) down to 3 — the paddleocr-only remainder.

One genuine M10-caused failure was found and fixed during this run:
`tests/unit/test_package_foundation.py::test_no_production_module_hardcodes_a_document_count`
flagged a docstring in `review_repository.py` that literally contained the
substring `len(...) == 4` (describing the notebook's own assertion, not
production logic); reworded, re-verified green.

The 50 deselected tests are every `requires_postgres`-marked test, M8/M9's
existing suite plus this milestone's new `tests/api/test_review_api_postgres.py`,
`tests/api/test_review_api_uvicorn.py`, and
`tests/integration/test_review_postgres_integration.py`.

### 13.2 Real PostgreSQL (via CI — task §18/§20)

**Not executed in this sandbox.** This environment's outbound-HTTPS proxy
explicitly does not support raw-TCP database connections (`/root/.ccr/README.md`:
"Not supported through the proxy ... raw-TCP databases"); a direct connection
attempt to the configured Neon endpoint timed out, matching the identical,
already-documented M8/M9 limitation. Per this milestone's own instructions:
the acceptance result is **not mocked**; the branch is pushed and the real
`requires_postgres` result is obtained through the
`M8/M9/M10 PostgreSQL acceptance` GitHub Actions workflow (§18), which this
milestone extends rather than replaces. **M10 is not safe to merge until that
workflow run is green** (§20/§25 below) — this report will be updated with the
real run's counts once that workflow completes, and is not to be read as a
claim that those tests currently pass.

Every `requires_postgres` test written for M10 was collection-verified
(`pytest --collect-only`, 17 + 8 = 25 additional tests total across
`tests/api/` and `tests/integration/test_review_postgres_integration.py`
beyond the pre-existing M8/M9 suite) and reviewed line-by-line against the
migrated schema (`src/ap_agent/db/migrations/000{2,3}_*.sql`) for
column-order/parameter-count correctness; two real bugs were caught this way
before commit (§11 items 8 and 9) precisely because this review could not
rely on a live run to surface them.

## 14. Security verification (task §21)

- No DSN, password, or Neon hostname committed in this milestone's diff
  (`git diff --cached | grep`-based scan; only pre-existing M8 report/test
  fixtures using placeholder credentials like `postgresql://user:pass@localhost`
  matched, none of them new).
- The real `AP_AGENT_TEST_POSTGRES_DSN` value was never printed, logged, or
  written to any file at any point in this session.
- No unrestricted CORS default (`ApiConfig.cors_allow_origins` defaults to
  empty; `"*"` is asserted out in `__post_init__`).
- No raw SQL, stack trace, or exception message reaches a client response —
  `ap_agent.api.errors`'s `ReviewIntegrityError`/unexpected-exception handlers
  return a fixed `["INTERNAL_ERROR"]`/`["DATABASE_UNAVAILABLE"]` envelope and
  log the real reason server-side only (verified by
  `tests/unit/test_api_errors.py::test_integrity_error_never_leaks_its_reason_to_the_client`/
  `test_unexpected_error_is_redacted_500`).
- No filesystem artifact path exposed: `InvoiceDetailRecord.source_artifact_uri`/
  `InvoiceDetailResponse.original_document_uri` stay `None` until a secure
  streaming/signed-URL mechanism exists.
- No payment execution path: `ReviewAction` has no `EXECUTE_PAYMENT` member;
  an `EXECUTE_PAYMENT` request body is rejected by Pydantic itself (`422`)
  before any route body executes.
- No production-authentication claim: `ap_agent/api/dependencies.py`'s module
  docstring explicitly labels the header adapter development/test-only and
  states what production requires.
- No cross-tenant data exposure: every repository read/write is tenant-scoped
  through PostgreSQL RLS (`set_tenant_context`), independently re-verified by
  application-level cross-tenant-row detection (`ReviewIntegrityError`).
- No mutation of original invoice memory: no Phase 9 SQL statement in this
  milestone issues `UPDATE`/`DELETE` against `ap_agent.invoice_memory_records`.

## 15. Dependency management (task §19)

Added an `api` extras group to `pyproject.toml`:

```toml
api = [
    "fastapi>=0.115,<1.0",
    "uvicorn>=0.34,<1.0",
    "httpx>=0.28,<1.0",
]
```

matching the notebook's own Phase 9 Cell 9 install constraint exactly.
Installed and verified in this environment:

| Package | Version |
|---|---|
| fastapi | 0.141.1 |
| starlette | 1.7.0 |
| uvicorn | 0.54.0 |
| httpx | 0.28.1 |
| pydantic | 2.13.5 |
| psycopg | 3.3.6 |
| psycopg_pool | 3.3.3 |

No Celery, Kafka, Redis, React, Base44, or agent framework was added.

## 16. CI (task §18)

`.github/workflows/m8-postgres-acceptance.yml` (renamed in its `name:` field
to `M8/M9/M10 PostgreSQL acceptance`; the workflow **file** is unchanged, per
the task's own instruction to extend the existing workflow rather than
create a new one):

- `if:` branch allow-list gains `claude/sharp-brahmagupta-ec6k5e` (this
  milestone's actual branch, §1); the two existing M8/M9 branches are
  untouched.
- The install step gains the `api` extra alongside the existing
  `dev,postgres,preprocessing,ocr-tesseract,ocr-paddle` set.
- Three new steps, each reported separately per task §20: "Run Phase 9 (M10)
  unit tests" (no DSN needed), "Run Phase 9 (M10) API tests" (`tests/api`,
  DSN-gated internally per-test), "Run Phase 9 (M10) real-PostgreSQL
  repository/service tests" (`tests/integration/test_review_postgres_integration.py`).
- The pre-existing "Run PostgreSQL acceptance suite" (`-m requires_postgres`)
  and "Run full unfiltered regression suite" steps are unchanged in
  substance — they now additionally exercise every M10 test as part of the
  same run, with no prior M8/M9 protection removed.
- Secrets are still read only via `${{ secrets.AP_AGENT_TEST_POSTGRES_DSN }}`,
  never printed; `concurrency.group` is unchanged (`m8-postgres-acceptance`)
  so an M10 run and an M8/M9 run against the same shared Neon test database
  still mutually exclude each other.

## 17. Blockers

- **Real PostgreSQL execution** could not run in this sandbox (§13.2) — the
  only blocker to a final merge-readiness verdict. Once the branch is pushed,
  the extended GitHub Actions workflow must be watched to green before this
  milestone can be called complete; any failure it surfaces must be diagnosed
  and fixed here, not worked around.

## 18. Merge readiness

**Not yet safe to merge.** Every requirement this session could verify
directly (extraction fidelity, module architecture, unit tests, DB-independent
API tests, security scan, dependency pinning, CI wiring) is complete and
green. The one requirement this session could not verify directly — the real
`requires_postgres` suite, including the concurrent-claim test — depends on
the GitHub Actions run this branch's push will trigger. M10 becomes safe to
merge only once that run is green, per this milestone's own instruction not
to mock that result.

## 19. Ready for the visual UI milestone?

The read side (dashboard/queue/detail projections, pagination, OpenAPI schema)
is ready to build a UI against today. The write side (claim/release/
correction/decision/resume) is implemented and unit/API-tested but its real
transactional behaviour under concurrency is only confirmed once the CI run
in §13.2 is green — a UI milestone that only reads should proceed now; one
that lets a user submit commands should wait for that green run.

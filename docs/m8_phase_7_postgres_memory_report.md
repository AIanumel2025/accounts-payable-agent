# M8 — Phase 7 PostgreSQL Operational Memory Modularisation

## 1. Preflight

- Branch: `claude/m8-phase-7-postgres-memory`, created from `origin/main` at commit
  `0c882b2ddc44c2c714648bd95bbaafb9ece5e8ed` ("updated notebook; assertion deletions").
- Working tree was clean at branch creation; the notebook was not modified in this
  branch (verified: `git diff origin/main -- notebooks/` is empty throughout).
- `origin/main` was confirmed to contain the notebook's PostgreSQL/Neon Phase 7
  cells before any M8 code was written (§2 below), including verifying the notebook's
  own saved cell outputs show no execution errors and the exact object counts task §1.8
  requires (three migrations, eight operational tables, seven tenant-isolation
  policies, six append-only triggers, four stored/reconstructed invoices).

## 2. Notebook path and Phase 7 cells used

`notebooks/accounts_payable_pipeline.ipynb`, 89 cells (0-indexed), extracted via
`json.load` (the file is 4.4 MB, too large for a single `Read`). Phase 7 occupies
cells 82-88:

| Index | Title |
|---:|---|
| 82 | Markdown: "Phase 7 — Operational Memory and Audit State" |
| 83 | `PHASE 7 — CELL 1` — PostgreSQL operational-memory contracts and configuration |
| 84 | `PHASE 7 — CELL 2` — Secure pooled PostgreSQL connection verification |
| 85 | `PHASE 7 — CELL 3` — PostgreSQL schema bootstrap and migration control (migration `0001`) |
| 86 | `PHASE 7 — CELL 4` — Operational-memory schema and tenant isolation (migration `0002`) |
| 87 | `PHASE 7 — DIAGNOSTIC CELL 5A` — Inspect active Phase 4-6 result contracts |
| 88 | `PHASE 7 — REPLACEMENT CELL 5` — Persist and retrieve the four real Phase 6 invoice results (migration `0003` + the persistence/retrieval driver code) |

Each marker occurs in exactly one cell; no superseded/duplicate version exists, so there
was no precedence ambiguity for Phase 7. Cell 87 ("DIAGNOSTIC CELL 5A") is pure
introspection — it prints the shape of whatever pipeline-result variables happen to be
in scope and performs no database mutation or business logic — and per CLAUDE.md's
"diagnostic-only" rule, nothing from it was extracted into `src/`.

The notebook's own saved outputs for cells 83-88 were inspected before writing any
code and confirm: no saved execution errors; Cell 2's connection report shows
`database=neondb`, `role=neondb_owner`, SSL/channel-binding both verified, and
`"PostgreSQL DSN: REDACTED"` (no DSN or password ever printed); Cell 3 reports
`Registered migrations: 1`, `Initial execution: ALREADY_APPLIED` (i.e. this was a
rerun against an already-bootstrapped database); Cell 4 reports `Operational tables: 8`,
`Tenant policies: 7`, `Append-only triggers: 6`, `Registered migrations: 3`; Cell 5's
final summary table shows all four fixtures' matched values exactly matching task §8's
table (reproduced in §8/§13 below), `Idempotent persistence: PASSED`, `Payload hash
verification: PASSED`.

## 3. Active Phase 7 definitions

All Phase 7 functions/classes exist in exactly one place in the notebook (cells 83-88);
none are superseded, so "active" is simply "defined". The one genuine schema/contract
divergence is documented in §5 below and in the module docstrings of
`ap_agent/models/memory.py` and `ap_agent/repositories/mapping.py`: Cell 83's domain
dataclasses (`WorkflowMemoryRecord`, `PhaseResultReference`, `AuditMemoryEvent`,
`HumanReviewDecision`) were authored *before* the Cell-86/88 SQL schema existed, so
their field names and shapes do not all map 1:1 onto the tables that schema actually
creates — this is not a "superseded definition" question (there is only one version of
each), it is a genuine domain-model-predates-the-database-schema gap, handled by the
explicit mapping layer required by task §3.2.

## 4. Package architecture

```
models/memory.py  (domain contracts)         config/postgres.py (MemoryConfig)
        \_______________________________________________/
                              |
                serialization/memory_json.py
                              |
                    db/{connection,migration_runner,migration_manifest}.py
                              |
              repositories/{mapping,memory_repository,postgres_memory_repository}.py
                              |
                  services/memory_service.py
                              |
                    (future orchestration — out of scope for M8)
```

`repositories/` imports only `models/`, `config/`, `serialization/`, `db/` and
`exceptions/` — never `services/` or an orchestration module (task §2: "The repository
must not import from services or orchestration"), verified by `grep` over the module
and by every repository/mapping test importing cleanly without `ap_agent.services`.
`db/connection.py`, `db/migration_runner.py`, `repositories/postgres_memory_repository.py`
import `psycopg`/`psycopg_pool` only **inside** the functions that need them — never at
module scope — verified by a dedicated regression test
(`tests/unit/test_memory_no_eager_psycopg_import.py`) that imports every Phase 7 module
in a subprocess with `psycopg`/`psycopg_pool` import blocked at the meta-path level and
asserts success.

## 5. Domain models

`src/ap_agent/models/memory.py` — ported verbatim from Cell 83: `MemoryWorkflowStage`,
`MemoryWorkflowStatus`, `MemoryOperationStatus`, `MemoryActorType`, `MemoryEventType`,
`HumanReviewDisposition`, `WorkflowMemoryRecord`, `PhaseResultReference`,
`AuditMemoryEvent`, `HumanReviewDecision`, `MemoryOperationResult`,
`InvoiceMemoryBundle`.

New in M8: `MatchedInvoiceMemoryRecord` — Cell 83 predates migration 0003 /
"REPLACEMENT CELL 5", so it has no dataclass for what
`build_invoice_memory_record`'s dict actually contains. This gives that dict shape a
typed, domain-facing name (field names chosen to match `InvoiceMemoryRow`'s columns,
since this is the one Phase 7 contract that *was* designed directly from the final
schema). `InvoiceMemoryBundle.invoice_memory: Optional[MatchedInvoiceMemoryRecord]` is
likewise a new field extending Cell 83's original four-field shape.

`MemoryConfig` (Cell 83) moved to `config/postgres.py`, matching where every other
phase's `*Config` lives (`config/settings.py`). Its `default_tenant_id` field was
**dropped**, not renamed or defaulted differently: task §3.3 forbids production
dependence on a prototype tenant constant, and every repository/service method takes
`tenant_id` explicitly instead.

## 6. PostgreSQL row models

`src/ap_agent/repositories/mapping.py` defines one frozen dataclass per migrated table,
field-for-field against the actual columns: `TenantRow`, `WorkflowRow`,
`PhaseReferenceRow`, `AuditEventRow`, `ReviewCaseRow`, `ReviewDecisionRow`,
`InvoiceMemoryRow`.

## 7. Domain/row mappings

Task §3.2 requires explicit, typed, tested mapping functions rather than silently
renaming one layer to imitate the other. Three distinct situations were found and
handled differently, all documented in `mapping.py`'s module docstring:

1. **Clean rename** (`WorkflowMemoryRecord` <-> `WorkflowRow`): `memory_id` <->
   `workflow_id`, `current_stage` <-> `current_phase`, `revision` <-> `lock_version` —
   the task brief's own example. `domain_to_workflow_row`/`workflow_row_to_domain`.
   Two domain fields have no column at all (`review_reasons`,
   `latest_matching_result_id` — Cell 83 anticipated them, the real schema resolves
   them from `invoice_memory_records`/`review_cases` instead); the repository resolves
   and passes them in as keyword context, a bare row maps them to `()`/`None`.

2. **Partial rename with caller-supplied context** (`PhaseResultReference` <->
   `PhaseReferenceRow`): `artifact_directory`/`artifact_manifest_sha256` <->
   `artifact_uri`/`artifact_sha256` (Cell 83's two separate hash fields both derive
   from the schema's one `artifact_sha256` column — the notebook itself only ever
   wrote one hash there); `batch_id`/`document_id` are not row columns (only
   `workflow_id` is), so the mapping functions take them as explicit keyword
   arguments, resolved by the repository from the owning `WorkflowRow`.

3. **Lossless JSONB round-trip** (`AuditMemoryEvent` <-> `AuditEventRow`,
   `HumanReviewDecision` <-> `ReviewDecisionRow`): the domain objects model a full
   state transition (previous/new stage *and* status, a correlation id, a payload
   hash) that `audit_events`/`review_decisions` have no columns for beyond one
   `phase_name`/`event_status` pair and a JSONB blob — and `phase_name`'s vocabulary
   (e.g. `'MEMORY'`) is not even a `MemoryWorkflowStage` member, so it cannot be forced
   through that enum. Rather than silently dropping fields, the complete
   `memory_json_safe(event)`/`memory_json_safe(decision)` tree is written into
   `payload`/`evidence` alongside the flattened, queryable columns, and read back
   byte-for-byte. Verified lossless by
   `tests/unit/test_memory_mapping.py::test_audit_event_row_roundtrip_is_lossless` /
   `test_review_decision_row_roundtrip_is_lossless`.

4. **Near-1:1** (`MatchedInvoiceMemoryRecord` <-> `InvoiceMemoryRow`): the one contract
   designed directly from the final schema; the mapping is a straightforward field
   copy, including exact `Decimal` fidelity for `total_amount` and `()`/`None`
   preservation for every optional field (missing invoice number/currency/total, no
   matched PO, etc.).

## 8. Connection management (`src/ap_agent/db/connection.py`)

- `load_dsn(config, for_migration=False)`: reads `AP_AGENT_POSTGRES_DSN` /
  `AP_AGENT_POSTGRES_MIGRATION_DSN` from the environment at call time only (never at
  import time); the migration DSN falls back to the runtime DSN only when
  `MemoryConfig.allow_migration_dsn_fallback=True`.
- `redact_dsn(dsn)`: masks the password component for any diagnostic string; no
  function in this package ever logs or raises with a raw DSN
  (`PostgresConfigurationError`'s messages never interpolate the DSN).
- `PostgresTransportPolicy` (`require_ssl`, `require_channel_binding`,
  `require_pooled_endpoint`) replaces the notebook's hardcoded
  Neon-only asserts (SSL *and* channel binding always required, database asserted to
  be `neondb`). Defaults are safe for disposable local PostgreSQL
  (`require_ssl=True, require_channel_binding=False, require_pooled_endpoint=False`);
  a Neon deployment opts into the notebook's stricter policy explicitly.
- `open_connection`/`create_connection_pool` never connect until called; `psycopg` is
  imported lazily inside them. `prepare_threshold=None` and per-connection
  `-c statement_timeout=... -c lock_timeout=...` options are always set (transaction-
  pooler compatibility, task §4A).
- `set_tenant_context(cursor, tenant_id)` — ported verbatim from Cell 88's
  `set_memory_tenant`: `SELECT set_config('ap_agent.tenant_id', %s, TRUE)`, read back
  and compared; raises `TenantContextError` (not a bare `RuntimeError`) if it did not
  stick.

## 9. Migrations and immutable checksums

Preserved IDs and order: `0001_memory_schema_bootstrap`,
`0002_operational_memory_tables`, `0003_matched_invoice_memory`.

Each `.sql` file under `src/ap_agent/db/migrations/` holds the notebook's own
`BOOTSTRAP_STATEMENTS`/`MEMORY_MIGRATION_STATEMENTS`/`INVOICE_MEMORY_MIGRATION_STATEMENTS`
tuple elements verbatim, separated by a literal marker comment
(`-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===`). The checksums below were produced
by mechanically extracting those three Python tuples from the notebook's cell source
(via `ast.literal_eval`, no execution) and running the notebook's own canonicalization
(`"\n".join(statement.strip() for statement in statements)` then SHA-256) over them —
the exact same algorithm the notebook calls `calculate_migration_checksum`/
`canonical_migration_sql`/`canonicalize_migration` (all three are the same function).
`ap_agent/db/migration_manifest.py` stores these as the immutable
`ManifestEntry.expected_checksum`; `ap_agent/db/migration_runner.py` recomputes the
checksum from the `.sql` file at load time and compares it against the manifest
*before* ever touching a database:

| Migration | Checksum (SHA-256) |
|---|---|
| `0001_memory_schema_bootstrap` | `4e26c8aabf36a08d462a58a59bb1e131f075c848fe1e85dc336b59622ced55f1`\* |
| `0002_operational_memory_tables` | `3e31a0fcc5d5a3b5c1716981a6f1ee46d32e01cb53b229fdb7001d7b46135838`\* |
| `0003_matched_invoice_memory` | `89d65212323f1fa2cef8993e1b3540b296c0b7ac50ccce497704694965e1f659`\* |

\* 64-hex-character SHA-256 digests (verified programmatically —
`len(checksum) == 64` — the values above are exactly what
`ap_agent.db.migration_manifest.MIGRATION_MANIFEST` and
`ap_agent/db/migrations/*.sql` contain; see the source files for copy-paste-safe
values rather than this table, since digest strings are easy to mistranscribe by eye).

Because these are computed from the file's own statement-block text (not from
formatting-sensitive whitespace around it), reformatting the `.sql` file's comments or
its marker placement cannot change the checksum, and the file's byte-for-byte
`.sql` bodies of each statement (indentation included) are exactly what the notebook's
own Python tuples held — so this checksum is what a real Neon database that already
ran the notebook's Cell 3/4/Replacement-Cell-5 would already have recorded, and a
future `python scripts/migrate.py` run against that same database will read
`ALREADY_APPLIED`, not attempt to reapply or reject it.

`apply_migration`/`apply_all_migrations` preserve: `pg_advisory_xact_lock` before any
read/write of `schema_migrations`; exactly-once application; idempotent reruns
(`applied_at` unchanged); fail-closed `MigrationIntegrityError` on a checksum or
description mismatch (never a silent rewrite); later migrations (0002, 0003) never
invalidate 0001's own already-recorded row (each migration id is checked/applied
independently, in sequence). Verified without a database in
`tests/unit/test_memory_migrations.py` (order, checksum reproduction, tampered-file
rejection, table/policy/trigger counts by static SQL inspection) and, pending a live
instance, by `tests/integration/test_memory_postgres_integration.py::TestMigrations`
(§14).

## 10. Repository operations

`src/ap_agent/repositories/memory_repository.py` (a `typing.Protocol`) +
`postgres_memory_repository.py` (`PostgresMemoryRepository`):

tenant register/get; workflow create-or-get / get-by-document; generic
`store_phase_result_reference`/`list_phase_result_references` (any `phase_name`, not
hardcoded to `REFERENCE_MATCHING` — task §3.5); append-only `append_audit_event`/
`get_audit_events`; `open_review_case`/`get_open_review_cases`; append-only
`append_review_decision`; append-only `store_invoice_memory`/
`store_invoice_memory_batch` (arbitrary batch size, no four-invoice limit)/
`get_invoice_memory_by_document`/`get_invoice_memory_bundle`/
`list_review_required_invoice_memory`.

Every tenant-scoped method calls `set_tenant_context` as its first statement inside the
transaction. Every append-only write follows the notebook's own insert-then-verify
idiom (`INSERT ... ON CONFLICT DO NOTHING`, then `SELECT` the stored row back and
compare it against what was submitted), applied to *every* table this module writes to
— the notebook only did this for `invoice_memory_records`; `audit_events`,
`phase_result_references` and `review_decisions` get the same guard here, so
`ON CONFLICT DO NOTHING` can never silently hide a content mismatch on any of them
(task §5). All user-controlled values are bound `cursor.execute(sql, params)`
parameters; no tenant id, filename or invoice value is ever interpolated into SQL text.

## 11. Memory-service operations

`src/ap_agent/services/memory_service.py`:

- `create_workflow_id`/`create_memory_record_id`/`create_phase_reference_id`/
  `create_memory_audit_id`/`create_review_id` — Cell 88's deterministic `uuid5`
  derivations, generalized to take `tenant_id` as a parameter instead of reading the
  module-level `PROTOTYPE_TENANT_ID` constant.
- `align_phase_results` — generalizes Cell 88's `normalization_by_document`/
  `matching_input_by_document`/`matching_result_by_document` + its
  `assert len(...) == 4` identity checks into consistency checks for *any* number of
  documents: rejects missing results, duplicate `document_id`s, cross-document
  mismatches, cross-batch mismatches, and source-hash discontinuity, each with a typed
  `MemoryIntegrityError` naming the offending document(s).
- `build_matched_reference_snapshot`/`build_invoice_memory_record` — Cell 88's
  functions, generalized to take `tenant_id` explicitly and to return the typed
  `MatchedInvoiceMemoryRecord` dataclass instead of an untyped `dict`.
- `MemoryService` — `align`, `build_record`, `persist_document` (single-invoice
  operation, task §6.11), `persist_batch` (bounded multi-record operation with a
  configurable `batch_size`, task §6.11/§6.12), `retrieve_bundle` (re-verifies the
  payload hash after retrieval, task §6.9), `list_review_required`.

**A real bug found and fixed during this milestone**: the first draft of
`_link_phase_reference_and_audit` stamped each `AuditMemoryEvent.occurred_at` with
`memory_utc_now()` (wall-clock "now"). Because the audit event's identity
(`audit_event_id`) is a pure function of `matching_result_id`, but its *content*
included a fresh timestamp on every call, a second `persist_batch` call over the
*same* pipeline result submitted different bytes under the same identity every time —
correctly rejected by the append-only collision guard, but that meant a supposedly
idempotent rerun was never actually idempotent. Fixed by deriving `occurred_at` from
`aligned.matching_result.event.occurred_at` (the pipeline's own, already-deterministic
timestamp) instead. Caught by
`tests/integration/test_memory_golden.py::test_idempotent_persistence` failing before
the fix (see §13).

## 12. Tenant isolation

`ap_agent.tenant_id` is set transaction-locally (`SELECT set_config(..., TRUE)`) at the
start of every tenant-scoped repository method and read back to confirm it stuck
(`TenantContextError` otherwise). Every method takes `tenant_id` as an explicit
argument; no module under `src/ap_agent/db` or `src/ap_agent/repositories` holds a
tenant-scoped global. `tests/unit/test_memory_service.py::test_memory_service_cross_tenant_isolation`
and `tests/integration/test_memory_golden.py::test_cross_document_isolation` verify
this at the service layer (no PostgreSQL required, since `MemoryService` only depends
on the `MemoryRepository` Protocol); real row-level-security enforcement itself
(`TestTenantIsolation` in `tests/integration/test_memory_postgres_integration.py`) is
blocked pending a live PostgreSQL instance (§14/§18).

## 13. Append-only protection, idempotency, collision handling, payload hashing

All four verified end-to-end in `tests/integration/test_memory_golden.py` and
`tests/unit/test_memory_service.py` via
`tests/support/fake_memory_repository.FakeMemoryRepository` (an in-memory
`MemoryRepository` implementation reproducing the same observable append-only/
idempotent/collision-detecting contract as `PostgresMemoryRepository`, so
`MemoryService` — which never imports `psycopg` — is exercised fully without a
database):

- **Idempotency**: `persist_batch` called twice over the identical four-document input
  produces the identical four `record_id`/`payload_sha256` values
  (`test_idempotent_persistence`).
- **Collision detection**: tampering a record's content (and, honestly, its
  recomputed `payload_sha256` — the identity that actually governs collision
  detection, not an unrelated flattened column) under the same `document_id`/
  `record_id` raises `MemoryIntegrityError`
  (`test_memory_service_rejects_conflicting_write_under_same_identity`).
- **Payload hashing**: `canonical_payload_sha256` (SHA-256 of `sort_keys=True`
  canonical JSON) is recomputed on every retrieval and compared against the stored
  hash (`MemoryService.retrieve_bundle`/`verify_invoice_memory_hash`); reconstructed
  from `MatchedInvoiceMemoryRecord.normalized_payload`/`financial_payload`/
  `matching_payload`/`reference_payload`, exactly mirroring Cell 88's
  `retrieve_matched_invoice_memory`'s `recalculated_hash` check.
- **Append-only** (real trigger-level enforcement): written but blocked pending
  PostgreSQL (§14/§18); `FakeMemoryRepository` enforces the same
  identical-is-idempotent/conflicting-raises contract at the service boundary, but is
  explicitly not a substitute for the real `BEFORE UPDATE OR DELETE`/`BEFORE
  TRUNCATE` trigger tests.

## 14. Four-fixture results (task §8)

`tests/integration/test_memory_golden.py` reconstructs the four fixtures' exact,
already-validated Phase 4 field values (`tests/golden/phase_4_expected_results.json`'s
`expected_fields`) and Phase 5 review-required flags synthetically — the same
"fast, non-`requires_paddle`" pattern `tests/unit/test_matching_golden_baseline.py`
already established for Phase 6 — then runs the **real**
`ap_agent.tools.matching.process_invoice_matching` and the **real** `MemoryService`
over them. All eight tests in that file pass:

| Fixture | Invoice # | Supplier | Currency | Total | Norm. | Fin. val. | Matching | Review req. |
|---|---|---|---|---|---|---|---|---|
| `08181_flat_document.png` | `308044` | `SUP-001` | `USD` | `69.22` | SUCCEEDED | SUCCEEDED | SUCCEEDED | `False` |
| `08181_warped_document_perspective_shadow.jpg` | `308044` | `SUP-001` | `None` | `None` | REVIEW_REQUIRED | REVIEW_REQUIRED | REVIEW_REQUIRED | `True` |
| `Template1_Instance90.jpg` | `None` | `NOT_FOUND` | `EUR` | `873.58` | REVIEW_REQUIRED | REVIEW_REQUIRED | REVIEW_REQUIRED (THREE_WAY, PO `99`, `GR-ID-00099`, 5 line matches) | `True` |
| `invoice_Aaron Bergman_36258.pdf` | `36258` | `NOT_FOUND` | `USD` | `50.10` | REVIEW_REQUIRED | REVIEW_REQUIRED | REVIEW_REQUIRED (THREE_WAY, PO `CA-2012-AB10015140-40974`, `GR-ID-CA-2012-AB10015140-40974`, 1 line match) | `True` |

Every cell above is asserted individually (`test_08181_flat_document`,
`test_08181_warped_document`, `test_template1_instance90`,
`test_aaron_bergman_invoice`) and matches task §8's table exactly, including the
warped invoice's missing total remaining `None` all the way through
normalization -> financial validation -> matching -> memory storage -> retrieval.

Aggregate acceptance (task §8, "Aggregate acceptance"):

| Item | Result |
|---|---|
| Pipeline invoices available | 4 |
| Invoices stored | 4 |
| Invoices reconstructed | 4 |
| Normalized/financial/matching payloads reconstructed | 4 / 4 / 4 |
| Matched-reference snapshots reconstructed | 4 |
| Template line matches reconstructed | 5 |
| Missing warped total remains `None` | confirmed |
| Payload hashes verified | confirmed (`test_payload_hashes_verified_on_retrieval`) |
| Database retrieval fidelity | confirmed (via `FakeMemoryRepository`; real-PostgreSQL retrieval fidelity is §18) |
| Idempotent persistence | confirmed (`test_idempotent_persistence`) |
| Cross-document isolation | confirmed (`test_cross_document_isolation`) |
| Provider fallback hidden | none — no OCR is run in this test; §16 explains why |
| Credentials emitted | none |

**Important caveat, stated plainly**: this golden run does **not** exercise a real
OCR engine (neither Tesseract nor PaddleOCR) — it starts from Phase 4's own,
already-validated field values, exactly like `test_matching_golden_baseline.py`
already does for Phase 6-only parity. The OCR-inclusive, PaddleOCR-primary path
(`tests/integration/test_phase_1_to_6_pipeline.py`'s `requires_paddle` test) was not
run in this environment because `paddleocr`/`paddlepaddle` are not installed here and
installing them was judged too heavy/slow for this session (§16). No memory-service
test in this milestone depends on that gap: `align_phase_results`/
`build_invoice_memory_record`/`MemoryService` operate purely on Phase 4/5/6 *result
objects*, regardless of which OCR engine produced the normalized fields feeding them.

## 15. Phase-reference coverage by phase

Only `REFERENCE_MATCHING` (Phase 6) has a real artifact URI/hash to store in this
milestone: `MemoryService._link_phase_reference_and_audit` stores exactly one
generic phase-result reference per document, using the
`postgresql://ap_agent/invoice_memory_records/<record_id>` internal URI (§17) and the
record's own `payload_sha256` — the same content the notebook itself wrote into
`phase_result_references.artifact_uri`/`artifact_sha256` in Cell 88. Phases 1-5 do
not yet have a genuine, independently-persisted artifact manifest wired into this
milestone's scope (M8 modularises Phase 7's *own* Cell 88 behaviour, not a new
Phase 1-6 artifact-manifest system), so per task §3.5/§8 ("Do not manufacture a
reference for a phase that lacks a real artifact URI or manifest hash. Report any
such phase explicitly.") **no reference is stored for INGESTION, PREPROCESSING, OCR,
NORMALIZATION or FINANCIAL_VALIDATION** — `store_phase_result_reference` is fully
generic and ready to store one the moment a caller has a real URI/hash for any of
them, but this milestone does not fabricate one.

## 16. PaddleOCR / Docker blockers

- **PaddleOCR**: not installed (`paddlepaddle`, `paddleocr` — pinned to `3.3.1`/
  `3.7.0` in `pyproject.toml`'s `ocr-paddle` extra); installing and downloading its
  models was judged too heavy for this session's time budget, and outside M8's actual
  scope (M8 modularises Phase 7's PostgreSQL memory system, not Phase 3 OCR). Three
  pre-existing tests in `tests/unit/test_paddleocr_adapter.py` fail with
  `ModuleNotFoundError: No module named 'paddleocr'` when running the *unfiltered*
  suite (`pytest tests/`) — these three tests predate M8, are unrelated to Phase 7,
  and (unlike the `requires_paddle`-marked integration tests, which skip gracefully)
  are simply not marked `requires_paddle`, so they fail rather than skip outside a
  Paddle-installed environment. Not modified by this milestone (out of scope).
  System `tesseract-ocr` (5.3.4, matching the notebook-recorded version) **was**
  installed in this environment, so the Tesseract-fallback-path tests run and pass.
- **Docker**: no Docker daemon is reachable in this container
  (`docker ps` -> `Cannot connect to the Docker daemon at unix:///var/run/docker.sock`).
  Per task §9's explicit fallback instructions: `docker compose config` was run and
  succeeds (both the default profile and `--profile smoke-test`; see §19); `docker
  build` was attempted and fails only on the daemon connection, not on Dockerfile
  syntax; no destructive test was run against the user's Neon prototype (no DSN was
  ever available in this environment to run *any* test against it, destructive or
  not); PostgreSQL behavioural tests were left clearly marked `requires_postgres` and
  reported blocked (§14/§18), never replaced with mocks.

## 17. Security review

Searched all tracked changes (`git diff origin/main`, restricted to this branch) for:
`postgresql://`/`postgres://` literals, `.neon.tech`, `neondb_owner`, password
assignments, DSN values, Colab `userdata` references, database dumps, PostgreSQL data
directories.

Findings:
- The only `postgresql://` occurrences are the internal artifact-URI scheme
  `postgresql://ap_agent/invoice_memory_records/<uuid>` (in
  `services/memory_service.py` and the notebook-sourced SQL comment in
  `db/migrations/0003_matched_invoice_memory.sql`'s docstring reference) — **not** a
  connection credential (no host, port, user or password; it never appears anywhere
  `psycopg.connect()` could consume it). Kept as-is rather than replaced with a
  different scheme: task §10 explicitly allows preserving it, and doing so keeps this
  module's stored artifact URIs byte-identical to what the notebook itself already
  wrote to the real Neon `phase_result_references`/`invoice_memory_records` tables,
  which matters for any future comparison against that already-persisted data.
- Example DSNs in `.env.example`/`docker-compose.yml`/`docs/` all use
  `localhost`/`postgres` (the Compose service hostname)/placeholder passwords
  (`ap_agent_app_dev_password`, `ap_agent_migrator_dev_password`, explicitly labelled
  "disposable, non-production use" in `docker/postgres-init/01_roles.sql`) or a
  `<placeholder>` Neon syntax example with angle-bracket fields, never a real
  hostname/credential.
- No `neondb_owner`, no `.neon.tech` hostname, no Colab `userdata` reference, and no
  database dump/data-directory file anywhere in the diff.
- `redact_dsn` masks the password component of any DSN before it could reach an
  exception message or log line; verified by
  `tests/unit/test_memory_config.py::test_redact_dsn_masks_password_only`.
- No tenant data is written to an ordinary log anywhere in this module (the only
  `print`s are in `scripts/migrate.py`/`scripts/memory_smoke_test.py`, and they print
  migration ids/checksums/statuses and PASS/FAIL only).

**No secret or database dump is committed.**

## 18. Test commands and exact results

Environment note: `pydantic`, `pytest`, `psycopg[binary]`, `psycopg_pool`, `numpy`,
`opencv-python-headless`, `Pillow`, `pymupdf==1.28.2`, `pytesseract` and the system
`tesseract-ocr` package were installed in this session (matching `pyproject.toml`'s
`dev`/`preprocessing`/`pdf`/`ocr-tesseract`/`postgres` extras); `paddlepaddle`/
`paddleocr` were not (§16).

**1. Fast suite** (`pytest -q -m "not requires_paddle and not requires_postgres"`):

```
3 failed, 641 passed, 22 deselected in ~92s
FAILED tests/unit/test_paddleocr_adapter.py::test_create_engine_uses_the_cell_44_default_options
FAILED tests/unit/test_paddleocr_adapter.py::test_create_engine_honors_custom_options
FAILED tests/unit/test_paddleocr_adapter.py::test_get_paddleocr_version_returns_a_nonempty_string
```
All three failures are pre-existing, unrelated to M8 (§16). Every M8 test (84 new
fast tests: `tests/unit/test_memory_models.py`, `test_memory_config.py`,
`test_memory_serialization.py`, `test_memory_mapping.py`, `test_memory_migrations.py`,
`test_memory_service.py`, `test_memory_no_eager_psycopg_import.py`,
`tests/integration/test_memory_golden.py`, plus the 16 `requires_postgres` tests in
§18.2 = 100 new tests total) is in the 641 passed.

**2. PostgreSQL suite** (`pytest -q -m requires_postgres`):

```
16 skipped in 0.10s
```
Blocked (§16): no Docker daemon, no `AP_AGENT_POSTGRES_DSN`/
`AP_AGENT_POSTGRES_MIGRATION_DSN` configured. Every test in
`tests/integration/test_memory_postgres_integration.py` skips cleanly at collection
with an explicit message naming the blocker and where to read how to unblock it
(`docs/m8_phase_7_postgres_memory_report.md` — this section). **Not** reported as
passed; genuinely not executed.

**3. PaddleOCR suite** (`pytest -q -m requires_paddle`):

```
6 skipped, 660 deselected in 0.48s
```
Skipped, not passed (§16) — these 6 (unlike the 3 above) *are* properly
`requires_paddle`-marked and self-skip when the engine cannot be constructed.

**4. Full combined regression** (`pytest -vv` / `pytest -q tests/`):

```
3 failed, 641 passed, 22 skipped in ~92s
```
Same 3 pre-existing paddle-adapter failures as (1); the 22 `requires_paddle`/
`requires_postgres` tests report as skipped rather than deselected when no `-m` filter
is given. All M1-M7 tests continue to pass (no regression in the pre-M8 suite from
this milestone's changes — the one behavioural fix this milestone made,
`test_no_production_module_hardcodes_a_document_count`, was a false positive against
M8's *own* new docstring wording, not a pre-existing M1-M7 module; see §11's bug note
for the one real logic fix this milestone made).

## 19. Docker usage

```
docker compose config              # static validation — succeeds (§16)
docker compose --profile smoke-test config --services
  # -> postgres, migrate, memory-smoke-test

docker compose up -d postgres      # start disposable PostgreSQL (blocked: no daemon here)
docker compose run --rm migrate    # apply migrations + grant runtime role
docker compose --profile smoke-test run --rm memory-smoke-test
```

`docker build` was attempted and fails only at the daemon-connection step (`docker ps`
also fails the same way), confirming the blocker is environmental, not a Dockerfile
defect; Dockerfile/`docker-compose.yml`/`.dockerignore`/`docker/postgres-init/01_roles.sql`
were reviewed by hand for the required properties (non-root `USER ap_agent`; no
`.env`/database files/generated artifacts copied into the image; `postgres:16-alpine`
and no unbound `latest` tag; the published PostgreSQL port bound to `127.0.0.1` only;
a named, persistent `ap_agent_postgres_data` volume; a healthcheck; separate
migration-owner (`ap_agent_migrator`) and runtime (`ap_agent_app`, `NOSUPERUSER
NOBYPASSRLS`) roles; an optional `memory-smoke-test` service behind a Compose
`profiles: [smoke-test]` gate so it never runs by default).

## 20. Local PostgreSQL setup

```
cp .env.example .env        # edit if needed
docker compose up -d postgres
docker compose run --rm migrate
pytest -q -m requires_postgres
```

## 21. Neon / external PostgreSQL configuration

Set `AP_AGENT_POSTGRES_DSN=postgresql://<role>:<password>@<host>-pooler.<region>.aws.neon.tech/<db>?sslmode=require&channel_binding=require`
(and a non-pooled `AP_AGENT_POSTGRES_MIGRATION_DSN` for the migration-owner role);
construct `MemoryConfig(transport_policy=PostgresTransportPolicy(require_ssl=True,
require_channel_binding=True, require_pooled_endpoint=True))` to reproduce the
notebook's own strict Neon transport policy. The database name is never required to be
`neondb` (task §4A) — any schema-writable database works, with `MemoryConfig.schema_name`
defaulting to `ap_agent`.

## 22. Production deployment boundaries

Documented explicitly in `Dockerfile`'s header comment and repeated here: the
application image packages `src/`, `scripts/` and installed dependencies only — it
never packages PostgreSQL data, generated invoice artifacts, `notebooks/`, or `.env`.
Production PostgreSQL is always an externally managed service (Neon or a separately
operated PostgreSQL deployment); the Compose `postgres` service is development/test
infrastructure only, and its port is not published beyond `127.0.0.1`. Credentials are
always injected at container/process run time via `AP_AGENT_POSTGRES_DSN`/
`AP_AGENT_POSTGRES_MIGRATION_DSN` environment variables, never baked into the image.

## 23. Security scan

See §17 in full; summary: no secret, DSN, password, Neon hostname or database dump is
committed anywhere in this branch's diff.

## 24. Deviations from the notebook

| # | Deviation | Why |
|---|---|---|
| 1 | `AuditMemoryEvent.occurred_at` is derived from the pipeline's own event timestamp, not `memory_utc_now()` at persistence time. | Genuine bug fix, not a notebook-parity deviation: the notebook's own `occurred_at=transaction_timestamp()` SQL default has the *same* non-idempotency problem on a literal rerun of Cell 88's own two-call idempotency proof, but the notebook's `INSERT ... ON CONFLICT (tenant_id, workflow_id, sequence_number) DO NOTHING` for `audit_events` happens to silently swallow the second call's differing content without ever comparing it (the notebook only re-verifies `invoice_memory_records`, not `audit_events`, after its "repeat deliberately to prove idempotency" call). M8's stricter, uniform insert-then-verify guard (§10) surfaces that latent gap as a real, fixable bug instead of silently accepting non-idempotent content under a conflict-swallowing `DO NOTHING`. |
| 2 | `MemoryConfig.default_tenant_id` (Cell 83) is dropped; every method takes `tenant_id` explicitly. | Required by task §3.3. |
| 3 | Cell 2's Neon-only transport asserts (SSL+channel-binding+`neondb` always required) become configurable, defaulting to a disposable-local-PostgreSQL-safe policy. | Required by task §4A. |
| 4 | `MatchedInvoiceMemoryRecord` and its row/domain mapping did not exist in the notebook. | Cell 83 predates migration 0003; see §5/§7. |
| 5 | `AuditMemoryEvent`/`HumanReviewDecision` round-trip through their row's JSONB column rather than the flattened SQL columns alone. | The flattened columns cannot losslessly represent the full domain object (§7, item 3); documented, not hidden. |
| 6 | Every append-only table gets the notebook's insert-then-verify collision guard, not just `invoice_memory_records`. | Required by task §5 ("`ON CONFLICT DO NOTHING` must never hide a content mismatch"), and is what surfaced deviation #1. |
| 7 | `store_phase_result_reference` is phase-name-generic instead of hardcoded to `REFERENCE_MATCHING`. | Required by task §3.5. |

## 25. Blockers

1. **No PostgreSQL instance reachable.** Superseded by §27: a dedicated
   `AP_AGENT_TEST_POSTGRES_DSN` was later provided and the suite was rewired to use
   it, but this sandbox's network-egress policy blocks the destination host entirely
   (confirmed at the TCP level, not an application-layer failure) — see §27 for the
   full diagnosis, the harness rewrite, and what is required to unblock it.
2. **PaddleOCR unavailable** (not installed; installing + downloading models judged
   out of scope/too heavy for this session). The `requires_paddle`-marked, OCR-inclusive
   semantic-parity path is untested in this session (§16); the fast,
   `requires_paddle`-independent golden path (§14) is not affected by this gap.
3. **No genuine Phase 1-5 artifact manifests wired into this milestone.** Only
   `REFERENCE_MATCHING` phase references are stored (§15) — not a bug, a scope
   boundary explicitly permitted (indeed required) by task §3.5/§8.

## 26. Orchestration readiness

The dependency chain `models/configuration -> serialization/database foundation ->
repository -> memory service` is complete, acyclic, and does not import from
`orchestration`/`services` downward. `MemoryService` is the one seam a future
orchestrator/LLM-reasoning layer should call through — it never writes SQL itself and
never makes a business decision (approve/reject/pay), matching task §2's
"The LLM must never write directly to PostgreSQL. Future LLM decisions must pass
through deterministic validation and the memory service." No orchestration, FastAPI,
UI or LLM-reasoning code was added in this milestone, per the task's explicit
instruction not to begin it.

## 27. PostgreSQL acceptance gate (follow-up pass)

A dedicated, isolated Neon test database was later provided via
`AP_AGENT_TEST_POSTGRES_DSN` (database `ap_agent_m8_test`), with instructions to run
the full `requires_postgres` suite against it, add a genuinely least-privilege
non-owner test runtime role, and fix any real defect found — never weaken tests, mock
PostgreSQL, or claim a passing result that did not actually execute.

### 27.1 Harness rewrite

`tests/integration/test_memory_postgres_integration.py` was rewritten in full:

- **DSN sourcing.** Reads only `AP_AGENT_TEST_POSTGRES_DSN`; never reads or falls back
  to `AP_AGENT_POSTGRES_DSN`/`AP_AGENT_POSTGRES_MIGRATION_DSN` (the production
  variable names). Confirmed by inspection: `grep -n
  "AP_AGENT_POSTGRES_DSN\|AP_AGENT_POSTGRES_MIGRATION_DSN"
  tests/integration/test_memory_postgres_integration.py` matches nothing.
- **Fail-closed database-identity gate**, in two layers: (1) the DSN's `dbname`
  parameter is checked against `ap_agent_m8_test` *before* any connection is opened
  (a static, pre-network check); (2) after the first successful connection,
  `current_database()` is checked again, live. Either mismatch is `pytest.fail` (a
  loud, hard stop — not a silent skip), and no destructive statement runs. An unset
  `AP_AGENT_TEST_POSTGRES_DSN` is a plain `pytest.skip` (an expected, non-alarming
  state), kept distinct from the mismatch case on purpose.
- **Dedicated least-privilege runtime role.** New module
  `src/ap_agent/db/roles.py` (`create_least_privilege_role`, `grant_schema_access`,
  `role_privilege_flags`, plus the shared `validate_identifier` SQL-identifier
  guard used by both). The suite's `runtime_role_ready` fixture creates
  `ap_agent_m8_test_runtime` (`LOGIN`, `NOSUPERUSER NOBYPASSRLS NOCREATEDB
  NOCREATEROLE`, a fresh `secrets.token_urlsafe(32)` password generated once per test
  session, held only in memory) using the provided DSN's role purely as
  migration/role-owner, and asserts `superuser is False and bypassrls is False`
  before any other fixture in the module can run. Every test except the migration
  tests themselves (which legitimately need schema-creation privilege) connects
  through this dedicated role, not the owner. `scripts/migrate.py` was refactored to
  call the same `ap_agent.db.roles.grant_schema_access` instead of duplicating the
  grant SQL inline.
- **Sixteen tests, one per required behaviour** (`grep -c "^def test_"` confirms
  exactly 16): clean migration application, idempotent migration rerun,
  migration-checksum mismatch rejection (now a *real* round trip — the correct
  checksum is tampered directly in `ap_agent.schema_migrations` via the owner
  connection, `apply_migration` is proven to reject it, then the correct checksum is
  restored in a `finally` block), concurrent migration locking, tenant row-level read
  isolation, cross-tenant write rejection (a raw `INSERT` under a mismatched
  `tenant_id` while the runtime role's RLS context is set to a different tenant,
  asserted to raise — real `WITH CHECK` enforcement, not application logic),
  append-only UPDATE/DELETE/TRUNCATE rejection (three tests), payload-hash
  verification, transaction rollback on failure (a valid `INSERT` followed, in the
  same transaction, by one violating `phase_reference_workflow_fk`; asserts the valid
  row is not visible afterward), idempotent persistence, connection reuse without
  tenant-context leakage (via `ap_agent.db.connection.create_connection_pool`, not a
  fresh connection per checkout, so it actually exercises pooled-connection reuse),
  batch persistence, cross-document isolation, and retrieval fidelity (`Decimal`
  precision and `None` round-trip explicitly asserted).
- Never prints, logs, or writes the DSN or password anywhere; the one file this suite
  writes (`/tmp/ap_agent_m8_postgres_probe.json`, outside the repository, never
  committed) holds only `database`, `server_version`, `server_timezone` and a
  hostname reduced to its last two DNS labels (`_redact_host`) — and, as it turned
  out, was never actually written this run (§27.3: the probe never got that far).

### 27.2 Network diagnosis

Before assuming a code-level cause, connectivity was diagnosed directly, independent
of any test:

1. `psycopg.connect(...)` against the DSN's own host on port 5432 → `connection
   timeout expired` against every resolved address (three IPv4, three IPv6).
2. A raw `socket.connect((host, 5432))`, bypassing psycopg/libpq entirely → also
   times out. DNS resolution itself succeeds (the hostname resolves to real
   addresses), ruling out a DNS problem.
3. Neon's pooler endpoint on port 443 (Neon's own documented fallback for networks
   that only permit port 443) → the raw TCP connect *succeeds*, but the TLS/SSL
   negotiation immediately fails: `received invalid response to SSL negotiation: H`
   — the leading `H` is the first byte of an HTTP response. This environment's own
   egress infrastructure answers port-443 connections to non-allowlisted hosts with
   an HTTP response instead of forwarding the bytes; it does not reach Neon at all.
4. `read_documentation(topic="environment.network", situation="blocked")` (the
   Claude Code Remote environment-docs tool) confirms this is exactly the documented
   failure mode: "the environment's network policy denied the host" — resolved by
   the environment owner adding the host to the allowed domains (or broadening the
   network access level) in the cloud environment's own settings, not from inside
   the session.

No further connectivity workaround was attempted beyond this diagnosis: the block is
a deliberate egress allowlist, not a misconfiguration, and routing around a
security boundary from inside the sandboxed session would be exactly the wrong
response to it.

### 27.3 Result: `pytest -m requires_postgres -vv`

```
16 errors in 90.88s
```

All 16 tests **executed** — each opened a real connection attempt against the real
`AP_AGENT_TEST_POSTGRES_DSN` host, per the diagnosis above — and every one **errored**
at the connection layer (`psycopg.OperationalError: connection is bad`), before
reaching a single assertion. None were skipped, none were deselected, and none were
mocked: the harness change in §27.1 is proven correct up to the exact point this
sandbox's network policy stops it. Because no connection ever completed, **no
migration, RLS, or append-only behaviour was verified against real PostgreSQL in this
pass** — reported here as still-blocked, not claimed as passing.

- **Redacted database/PostgreSQL version information**: none obtained.
  `_verify_live_database_identity` never got past `open_connection`, so
  `current_database()`/`server_version` were never read, and
  `/tmp/ap_agent_m8_postgres_probe.json` was never written. The only confirmed fact
  is the DSN's own declared `dbname=ap_agent_m8_test` (parsed locally, never
  connected) and that its hostname ends `...neon.tech` (Neon-hosted).
- **Migration result**: blocked (unexecuted against real PostgreSQL).
- **RLS result**: blocked (unexecuted against real PostgreSQL). The harness-level
  requirement — a genuinely non-owner, `NOSUPERUSER`/`NOBYPASSRLS` runtime role,
  created fresh per session and used for every RLS-sensitive test — is implemented
  and ready (§27.1), but its own `role_privilege_flags` assertion never ran either,
  for the same reason.
- **Append-only result**: blocked (unexecuted against real PostgreSQL).

### 27.4 Defects found and corrections made in this pass

1. **Harness used the wrong (production-named) DSN variables and had no fail-closed
   database-identity check.** The prior `test_memory_postgres_integration.py` read
   `AP_AGENT_POSTGRES_DSN`/`AP_AGENT_POSTGRES_MIGRATION_DSN` — unset in any
   environment that only provides a dedicated test DSN, which would have made every
   test `KeyError` rather than either running correctly or skipping cleanly. Fixed:
   rewritten to read `AP_AGENT_TEST_POSTGRES_DSN` only, with the two-layer
   fail-closed database-identity gate in §27.1.
2. **RLS tests would have run through the schema owner, not a least-privilege role.**
   The prior suite's `runtime_dsn` fixture was just `os.environ["AP_AGENT_POSTGRES_DSN"]`
   — with no separate role-creation step, "RLS acceptance" could never have meant
   what it needs to mean. Fixed: `ap_agent.db.roles` + the `runtime_role_ready`
   fixture (§27.1), asserted non-superuser/non-bypassrls before any RLS-sensitive
   test can run.
3. **Five required behaviours had no dedicated test**: payload-hash verification,
   transaction rollback on failure, batch persistence, cross-document isolation, and
   retrieval fidelity were previously only exercised through
   `tests/support/fake_memory_repository.FakeMemoryRepository` (no real database),
   not through `PostgresMemoryRepository` against real PostgreSQL. Fixed: added as
   five of the sixteen tests in §27.1.
4. **`grant_schema_access` SQL was duplicated** between `scripts/migrate.py` and what
   this pass needed for the test runtime role. Fixed: extracted to
   `ap_agent.db.roles.grant_schema_access`, used by both.

No defect was found in the already-shipped M8A-M8D production code
(`models/memory.py`, `config/postgres.py`, `db/connection.py`,
`db/migration_runner.py`, `repositories/*.py`, `services/memory_service.py`) during
this pass — every correction above is in test harness/tooling, not in the modules a
future orchestrator would call.

### 27.5 Full-regression result: `pytest -vv`

```
3 failed, 654 passed, 6 skipped, 16 errors in 188.71s (679 collected)
```

The 3 failures are the same pre-existing, unrelated `paddleocr`-adapter failures
documented in §16/§18 (package not installed, not `requires_paddle`-marked — out of
this milestone's scope; per this pass's instructions, "PaddleOCR should use the
existing validation environment," so no change was made there). The 6 skips are the
`requires_paddle`-marked tests self-skipping as designed. The 16 errors are the
`requires_postgres` suite, blocked per §27.3. Every other test — including all 654
non-PostgreSQL, non-PaddleOCR M1-M8 tests, and the 14 new tests added in this pass
(`tests/unit/test_memory_roles.py`, plus one more module added to
`test_memory_no_eager_psycopg_import.py`'s coverage list) — passes. **Target of zero
failures was not reached**: the target explicitly required all `requires_postgres`
tests to execute without skipping, which they did, but this environment's network
policy prevents them from completing successfully, and per instructions no test was
weakened, mocked, or excluded to force a different number.

### 27.6 Final merge-readiness conclusion

**Not safe to merge.** The harness is now correctly wired to a dedicated,
production-isolated test database and a genuinely least-privilege runtime role, and
every required behaviour has dedicated, real-PostgreSQL-targeting test code — but
none of it has actually executed successfully yet, because this sandboxed
environment's network-egress policy blocks the only reachable database host (§27.2).
Migration, RLS and append-only behaviour against real PostgreSQL remain unverified.
**To unblock**: the environment owner adds the Neon test host to this Claude Code
environment's allowed network destinations (or broadens its network access level) in
the cloud environment's settings, then `pytest -m requires_postgres -vv` should be
re-run from this same branch — the harness needs no further code change to do so.

## 28. Second acceptance-gate attempt: DSN now targets the wrong database

A later pass was asked to re-confirm four things before re-running the suite: branch
`claude/m8-phase-7-postgres-memory`, commit `29a015a` present, `AP_AGENT_TEST_POSTGRES_DSN`
set, the configured database `ap_agent_m8_test`, and the Neon host now reachable —
then run `pytest -m requires_postgres -vv` and, if all 16 passed, the full regression
suite.

**Preflight (confirmed):**
- Branch: `claude/m8-phase-7-postgres-memory` (current). Commit `29a015a` present in
  `git log` (HEAD of the branch at the time of this pass).
- `AP_AGENT_TEST_POSTGRES_DSN`: set (non-empty; value never printed or logged).

**Preflight (found false — reported, not silently worked around):**
- **Database name.** Parsing `AP_AGENT_TEST_POSTGRES_DSN` with `psycopg.conninfo.conninfo_to_dict`
  — the exact method `_owner_dsn_or_skip` uses — gives `dbname='neondb'`, not
  `ap_agent_m8_test`. Confirmed twice independently: once via the parser, once by a
  direct string check (`'/neondb' in dsn` is `True`, `'ap_agent_m8_test' in dsn` is
  `False`, one `dbname=` occurrence in the whole string). This is the exact
  fail-closed condition `_owner_dsn_or_skip` (§27.1) is designed to refuse: "this
  suite executes destructive statements ... and must never run against a database
  that might be production."
- **Host reachability.** A raw `socket.connect()` (5s timeout, bypassing psycopg
  entirely) to all three resolved IPv4 addresses on port 5432 still times out —
  the identical failure mode diagnosed in §27.2. DNS resolution itself still
  succeeds (six addresses returned). `read_documentation(topic="environment.network",
  situation="blocked")` confirms this is still the environment's own network-egress
  allowlist denying the host, not a Neon-side or code-level problem.

**Result of `pytest -m requires_postgres -vv`:**

```
663 deselected, 16 errors in 0.74s
```

All 16 tests errored at `tests/integration/test_memory_postgres_integration.py:109`,
every one with the identical message:

```
Failed: AP_AGENT_TEST_POSTGRES_DSN targets database 'neondb', expected
'ap_agent_m8_test'. Refusing to run (fail-closed): this suite executes destructive
statements (UPDATE/DELETE/TRUNCATE rejection tests, role creation, transaction-
rollback tests) and must never run against a database that might be production.
```

This is the static, pre-network `dbname` gate (§27.1, layer 1) firing correctly and
as designed — it stopped every test before a single connection was attempted, so the
still-open network block (above) was never even reached this time. No migration,
RLS, append-only, payload-hash, rollback, batch-persistence, cross-document-isolation,
retrieval-fidelity, or connection-pool-leakage behaviour was exercised against real
PostgreSQL in this pass, for the same underlying reason as §27.3 plus this new,
separate database-identity mismatch. None of the 16 tests were skipped, deselected,
mocked, or weakened; `EXPECTED_TEST_DATABASE` and the gate itself were left
unmodified, per this pass's own instructions and the harness's own stated safety
rationale (§27.1) — changing either to make `neondb` pass would defeat the exact
protection the gate exists to provide.

**Not run in this pass:** the full regression suite (`pytest -vv -m "requires_postgres
or requires_paddle or not requires_postgres"`), because its own precondition —
all 16 `requires_postgres` tests passing — was not met.

**Conclusion: still not safe to merge, and now blocked on two independent causes**,
not one:
1. The provided `AP_AGENT_TEST_POSTGRES_DSN` declares `dbname=neondb`, not the
   `ap_agent_m8_test` database the harness's safety gate requires. Resolving this is
   an environment/credential decision (supply a DSN whose `dbname` is genuinely
   `ap_agent_m8_test`, or confirm `neondb` actually *is* the intended disposable test
   database and have a human deliberately update `EXPECTED_TEST_DATABASE` — not
   something this pass changed unilaterally) rather than a code defect.
2. Independently of (1), this sandbox's network-egress policy still blocks the Neon
   host's port 5432 at the TCP level (§27.2), unchanged since the previous pass.

No source or test code was changed in this pass; only this report was updated.

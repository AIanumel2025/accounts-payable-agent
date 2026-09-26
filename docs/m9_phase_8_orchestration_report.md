# M9 — Phase 8 Workflow Orchestration Modularisation

## 1. Preflight

- Branch: `claude/m9-phase-8-orchestration`, created from `origin/main` at commit
  `96cfb5e047674ba3f75b4f4e9211f32fec7b1973` ("updated notebook").
- Working tree was clean at branch creation; the notebook was not modified in this
  branch (verified before and after all work: `git diff origin/main -- notebooks/`
  is empty).
- The notebook (`notebooks/accounts_payable_pipeline.ipynb`, 100 cells, 0-indexed)
  was confirmed to contain, before any M9 code was written: Phase 7's corrected
  current-document retrieval scoping (cell 88), Phase 8 Cells 1-5 (cells 90-93, 98)
  and correction cells 4A-4D (cells 94-97), and Phase 8 Cell 5's own successful
  final real-orchestration output (§8 below).

## 2. Notebook path and Phase 7/8 cells used

`notebooks/accounts_payable_pipeline.ipynb`. Phase 7 occupies cells 82-88; Phase 8
occupies cells 89-98.

| Index | Title | Role |
|---:|---|---|
| 88 | `PHASE 7 — REPLACEMENT CELL 5` | Corrected current-document retrieval scoping (`retrieve_matched_invoice_memory`); audited in §5 below |
| 90 | `PHASE 8 — CELL 1` | Contracts and configuration — **active**, extracted verbatim into `ap_agent.models.orchestration` |
| 91 | `PHASE 8 — CELL 2` | Transition graph, routing, failure classification — **active**, extracted verbatim into `ap_agent.orchestration.routing` |
| 92 | `PHASE 8 — CELL 3` | Framework-neutral execution engine — **active**, extracted verbatim (control flow) into `ap_agent.orchestration.engine` |
| 93 | `PHASE 8 — CELL 4` | Real Phase 1-7 tool bindings — **superseded in part** by 4A/4B/4C/4D; its five phase-input bridge functions and the `NotebookPhaseToolBindings` class *shape* are the active reference, its uncorrected `handler_mapping`/`run_financial_validation`/`run_reference_matching` are not (see §4) |
| 94 | `PHASE 8 — CORRECTION CELL 4A` | Handler-call signature adapter — **notebook-only shim**, not ported (§4) |
| 95 | `PHASE 8 — CORRECTION CELL 4B` | Upstream-result duck-typed resolution — **notebook-only shim**, not ported (§4) |
| 96 | `PHASE 8 — CORRECTION CELL 4C` | Phase 5 notebook-helper isolation — **active** for its structured-failure exposure and its restated Phase 5 helper *behaviour* (quantization, tolerance, reason handling); its `globals()`-swapping *mechanism* is not ported (§4) |
| 97 | `PHASE 8 — CORRECTION CELL 4D` | Phase 6 notebook-helper isolation — same split as 4C (§4) |
| 98 | `PHASE 8 — CELL 5` | Real orchestration and validation — **active**; its `workflow_result_route` fallback (current_stage → route) is the source for `InvoiceWorkflowResult.terminal_route` (§8); its golden numbers are `tests/golden/phase_8_expected_results.json`'s source |

No Phase 8 marker occurs more than once; there was no cell-numbering precedence
ambiguity. The precedence decision that *does* matter is 4A/4B/4C/4D's split
between "behaviour to keep" and "notebook-namespace mechanism to discard" — see §4.

## 3. Extracted contracts (`ap_agent/models/orchestration.py`)

Ported verbatim from cell 90: `OrchestrationStage`, `StageExecutionStatus`,
`InvoiceWorkflowStatus`, `BatchWorkflowStatus`, `OrchestrationFailureClass`,
`WorkflowRoute`, `OrchestrationEventType`, `ORCHESTRATION_PROCESSING_ORDER`,
`ORCHESTRATION_TERMINAL_STAGES`, `OrchestrationRetryPolicy`,
`OrchestrationConfig`, `InvoiceWorkflowRequest`, `BatchWorkflowRequest`,
`StageExecutionRecord`, `OrchestrationEvent`, `WorkflowCheckpoint`,
`InvoiceWorkflowResult`, `BatchWorkflowResult`, `orchestration_utc_now`,
`calculate_retry_delay_seconds`.

From cell 93: `MemoryPersistenceStageResult` (verbatim field set).

New in M9: `StructuredPhaseFailure` (§12), `InvoiceWorkflowResult.terminal_route`
(§8), and `default_orchestration_config`/`default_orchestration_retry_policy`
functions replacing the notebook's two module-level instances
(`orchestration_config`, `orchestration_retry_policy`) — CLAUDE.md forbids a
mutable module-level config *instance*; every value is identical, only the
construction is now a function call a caller makes explicitly.

## 4. Notebook-shim decision (task §4; the central M9 design decision)

Correction cells 4A/4B/4C/4D exist purely because every notebook cell shares one
Python process's `globals()`. Three concrete problems they patch, and how the
package avoids each without patching anything at runtime:

1. **4A — handler call signature.** Cell 93's real handlers take
   `(request, context, attempt_number)`; the Cell 3 engine calls
   `handler(context)`. 4A wraps every handler in `*args`/`**kwargs`-sniffing
   code to bridge the two. **Not needed**: `ap_agent.orchestration.handlers
   .ProductionPhaseBindings`'s seven `run_*` methods are written directly
   against the engine's one-argument `StageHandler` contract from day one —
   there is no mismatch to adapt away.
2. **4B — upstream-result lookup.** Cell 93's handlers call a
   `get_context_result(context, "normalization_result")` written before the
   real `WorkflowExecutionContext` shape existed, so 4B rewrites it to probe six
   different guessed container names, direct attributes, accessor methods and
   tuple-of-pairs collections. **Not needed**: `ap_agent.orchestration.engine
   .WorkflowExecutionContext.result_for(stage)` is one direct dict lookup —
   `ProductionPhaseBindings` calls it knowing the exact shape, because both live
   in this package.
3. **4C/4D — helper collisions.** By the time Phase 8 re-runs Phase 5's/Phase
   6's real functions, later notebook cells have already reassigned same-named
   helpers those functions call (Phase 7 overwrites Phase 6's one-argument
   `normalized_field_value` with a two-argument function of the same name; Phase
   6 and Phase 5 also collide on `append_unique_reason`/`quantize_money`/etc.
   across cells). 4C/4D each open a `contextlib.contextmanager` that temporarily
   `globals().update(...)`s the *original* Phase 5/6 definitions back in,
   `yield`s, then restores whatever was there afterward. **Not needed**: every
   Phase 4-7 tool module (`ap_agent.tools.normalization`,
   `ap_agent.tools.financial_validation`, `ap_agent.tools.matching`,
   `ap_agent.serialization.memory_json`) already keeps its own
   module-private helper under Python's ordinary import scoping. Concretely:
   `ap_agent.tools.matching.normalized_field_value` takes one argument
   (`normalized_field`); `ap_agent.serialization.memory_json
   .normalized_field_value` takes two (`invoice_record, requested_field_name`).
   They are different function objects in different modules' `__dict__`s — one
   cannot overwrite the other regardless of import order.
   `tests/integration/test_orchestration_namespace_isolation.py` proves this by
   importing all of them together and running the two PO-backed real fixtures
   (`Template1_Instance90.jpg`, `invoice_Aaron Bergman_36258.pdf`) through real
   Phase 1-6 line matching: no `TypeError`, both `REVIEW_REQUIRED`.

What *was* kept from 4C/4D is their other job: exposing a structured Phase 5/6
`FAILED` result's diagnostics instead of losing them. The notebook does this by
re-raising a bare `RuntimeError` (whose message joins the phase's `errors` with
`" | "`) — which, fed back through Cell 2's `classify_orchestration_failure`,
resolves to `OrchestrationFailureClass.UNKNOWN` with the phase's individual
`errors`/`review_reasons` collapsed into one synthesized string. Task §12
requires better than that; see §12.

Confirmed no production orchestration module contains `globals()` mutation
(`tests/integration/test_orchestration_namespace_isolation
.py::test_no_production_orchestration_module_swaps_globals_at_runtime`) or a
`contextlib.contextmanager`-based scope shim.

## 5. M8 Phase 7 retrieval audit (task §5)

**Finding: M8 already satisfies the policy. No M8 production code was changed.**

The notebook's corrected `retrieve_matched_invoice_memory` (cell 88) reads:

```sql
WHERE tenant_id = %s AND document_id = ANY(%s::uuid[])
```

— scoped by tenant identity *and* an explicit current-document-ID collection,
never an unscoped `SELECT * FROM invoice_memory_records WHERE tenant_id = %s`.

Every M8 read path already has this shape:

- `PostgresMemoryRepository.get_invoice_memory_by_document` /
  `.get_workflow_by_document` / `.get_invoice_memory_bundle` are each keyed by
  the explicit pair `(tenant_id, document_id)` — one document, one call. A
  caller wanting "this run's documents" calls it once per explicit ID in its own
  current-run set (exactly what
  `ap_agent.orchestration.handlers.ProductionPhaseBindings
  .run_memory_persistence` does via `MemoryService.persist_document`, and what
  `MemoryService.retrieve_bundle` does for a caller reading it back). That is
  the same "constrained by explicit document identity" shape as
  `document_id = ANY(...)`, issued as N single-document queries instead of one
  batched query.
- The one method that scans more than one document per call,
  `list_review_required_invoice_memory(tenant_id=...)`, is still
  tenant-scoped *and* filtered to `review_required = TRUE` — a distinct,
  intentional "open worklist" query, not "current-run validation" (the line
  task §5 itself draws). It never returns a tenant's full row set.
- Neither path assumes a fixed document count (CLAUDE.md D-2/D-3), and neither
  relies on `batch_id` for scoping (every real query is keyed by
  `document_id`, so an idempotent record retaining an earlier `batch_id` cannot
  affect retrieval).

New tests (no M8 production code touched):

- `tests/integration/test_m9_m8_retrieval_audit.py` (fast, `FakeMemoryRepository`,
  5 tests): 11 unrelated historical rows + a 3-document "current run" under one
  tenant → current-run retrieval returns exactly those 3, historical rows remain
  stored; a historical row sharing the current run's own `batch_id` is not
  returned unless its `document_id` is explicitly requested; cross-tenant
  isolation holds even with a colliding `document_id`; payload-hash
  verification (`MemoryService.retrieve_bundle`'s
  `verify_invoice_memory_hash`) still passes alongside historical noise;
  `list_review_required_invoice_memory` returns only review-flagged rows, not
  every row.
- `tests/integration/test_memory_postgres_integration.py::
  test_m8_retrieval_audit_current_run_ids_only` /
  `::test_m8_retrieval_audit_cross_tenant_isolation_with_colliding_document_id`
  (`requires_postgres`, appended to the existing M8 acceptance suite, reusing
  its `repository`/`tenant` fixtures): the same two scenarios against real
  PostgreSQL/RLS. `AP_AGENT_TEST_POSTGRES_DSN` is unset in this environment (see
  §17 for the blocker this repeats from M8), so these two collect correctly but
  do not execute here; `.github/workflows/m8-postgres-acceptance.yml`'s branch
  condition was extended to include `claude/m9-phase-8-orchestration` so they
  run in CI on this PR.

## 6. Module layout

```text
src/ap_agent/
├── models/
│   └── orchestration.py       # contracts, terminal-route derivation, StructuredPhaseFailure
├── orchestration/
│   ├── __init__.py             # empty (C-5: no eager re-exports)
│   ├── routing.py              # transition graph, status normalisation, failure classification, route_stage_result
│   ├── engine.py                # StageHandlerRegistry, WorkflowExecutionContext, execute_invoice_workflow
│   ├── handlers.py              # ProductionPhaseBindings: M3-M8 tool bindings
│   ├── batch.py                  # execute_batch_workflow: bounded concurrency, ordering, isolation
│   └── observability.py          # EventSink, CollectingEventSink, ConsoleProgressObserver, formatters
```

No circular imports: `orchestration.{routing,engine}` → `models.orchestration`
only; `orchestration.handlers` → `orchestration.engine` + `models.orchestration`
+ `config.settings` + `models.matching`/`validation` + `services.memory_service`
+ `tools.*`; `orchestration.batch` → `orchestration.engine` + `models
.orchestration`; `orchestration.observability` → `models.orchestration` only.
Nothing under `models/` imports `orchestration/`, `config/` or `exceptions`.

## 7. Routing graph

```
INGESTION → PREPROCESSING → OCR → NORMALIZATION → FINANCIAL_VALIDATION
  → REFERENCE_MATCHING → MEMORY_PERSISTENCE → {COMPLETED | HUMAN_REVIEW}
```

Same-stage retry is legal for any processing stage. `HUMAN_REVIEW`/`COMPLETED`
are terminal (no further transition is legal from either, verified by
`test_terminal_stages_reject_every_further_transition`). An unrecognised result
status (no status-shaped attribute at all) fails closed to `STOP_FAILED` with
`OrchestrationFailureClass.INTEGRITY` and `review_required=True` — the workflow
never silently continues on an unreadable result.

## 8. Retry policy

`maximum_attempts=3`, `initial_delay_seconds=1.0`, `backoff_multiplier=2.0`,
`maximum_delay_seconds=8.0`; only `TRANSIENT` failures retry by default
(`retry_integrity_failures`/`retry_policy_failures`/`retry_permanent_failures`
all `False`). Delay schedule: attempt 1 → 0s, 2 → 1s, 3 → 2s, 4 → 4s (capped at
8s from attempt 5 on) — verified against the notebook's own asserted values.
`ap_agent.orchestration.engine.default_retry_waiter` does not block (matches
the notebook's `notebook_retry_waiter`); `blocking_retry_waiter` is provided for
a caller that wants real-time in-process waiting, and any caller may inject its
own `RetryWaiter` (a durable Temporal/queue timer, for the "clean extension
point" task §10 asks for without adding a framework dependency now).

## 9. Failure classification

Exception name/message markers and `isinstance` checks, ported verbatim from
cell 91: `AssertionError` → `INTEGRITY`; `FileNotFoundError`/`PermissionError`/
`TypeError`/`ValueError`/`UnicodeError`/`NotImplementedError` → `PERMANENT`;
`TimeoutError`/`ConnectionError` (and message markers like "connection reset",
"rate limit") → `TRANSIENT`; class names containing "Policy"/"Approval"/
"Authorization"/"Permission" → `POLICY`; anything else → `UNKNOWN` (never
retried).

## 10. Terminal-route resolution (task §8)

`InvoiceWorkflowResult.terminal_route` is a `@property`, not a stored field: it
maps `current_stage == COMPLETED → "COMPLETED"`, `current_stage ==
HUMAN_REVIEW → "HUMAN_REVIEW"`, anything else → `"NONE"`. There is exactly one
source of truth (`current_stage`); the engine guarantees a `SUCCEEDED` result's
`current_stage` is always `COMPLETED` and a `REVIEW_REQUIRED` result's is always
`HUMAN_REVIEW`, so a successful workflow can never resolve to `"NONE"` (proven
by `test_successful_workflow_never_exposes_none_as_terminal_route` and the
four-fixture acceptance suite, where every result's `terminal_route` matches the
golden table exactly). Internal routing still uses `WorkflowRoute.COMPLETE`/
`WorkflowRoute.ROUTE_TO_REVIEW` for its own decision-making, unchanged.

## 11. Structured-failure handling (task §12)

`ap_agent.orchestration.handlers.ProductionPhaseBindings.run_financial_validation`
/`run_reference_matching` check for a Phase 5/6 `FAILED` status and raise
`StructuredPhaseFailure(stage, errors=result.errors,
review_reasons=result.review_reasons, failure_class=INTEGRITY)` instead of
continuing to the next phase. `execute_invoice_workflow` has a dedicated
`except StructuredPhaseFailure` branch: it classifies using the exception's own
`failure_class` (not a generic name/message probe), appends *each* of its
`errors` individually to `context.errors` (never collapsed into one string, and
never `errors=()`), and carries its `review_reasons` into the stage's routing
decision. `execute_invoice_workflow`'s existing STOP_FAILED handling still
applies (downstream stages never run). This is a deliberate, documented
improvement over the notebook's own bare-`RuntimeError` re-raise (which the
generic exception path would classify as `UNKNOWN` with the diagnostics
collapsed) — it changes no outcome for the four real fixtures, since none of
them hits this path (§2's golden table has zero `FAILED` outcomes). Tested by
`tests/unit/test_orchestration_engine.py
::test_structured_phase_failure_preserves_errors_and_review_reasons` /
`::test_structured_phase_failure_is_not_classified_as_unknown` (synthetic) and
implicitly by the four-fixture suite's `zero failed stages` assertion (the real
path is never taken there, which is itself the expected/golden behaviour).

## 12. Batch design (task §13)

`ap_agent.orchestration.batch.execute_batch_workflow`: rejects a batch over
`OrchestrationConfig.maximum_batch_documents` (default 1000) before touching the
handler registry at all (`BatchSizeExceededError`); builds one independent,
deterministic `InvoiceWorkflowRequest` per document
(`derive_invoice_correlation_id` is a `uuid5` of `(batch_id, filename)`, stable
across reruns); runs invoices on a `ThreadPoolExecutor` bounded by
`min(max_workers or config.maximum_batch_concurrency, config
.maximum_batch_concurrency, len(invoice_requests))` (never exceeds the
configured ceiling, defaulting to 4 — the "safe execution mode for real OCR
providers" task §13 asks for); collects results into a pre-sized list indexed
by input position (never by completion order), so `BatchWorkflowResult
.invoice_results` always matches `request.source_paths`' order regardless of
which invoice finishes first; an unhandled exception from one invoice (a defect
that escapes the engine's own per-stage isolation) is caught per-invoice and
converted to a terminal `FAILED` `InvoiceWorkflowResult`, never aborting the
batch or losing another invoice's already-completed result.
`tests/unit/test_orchestration_batch.py::
test_two_hundred_document_synthetic_batch_completes_correctly` runs 200
synthetic documents (3 requiring review, 1 a permanent failure, scattered
non-contiguously — never assuming four or any fixed count), asserts bounded
concurrency, stable ordering, correct aggregate counts, and per-invoice
isolation, in well under a second of wall-clock stage-handler work.

## 13. Observability design (task §14)

No core orchestration module (`routing.py`, `engine.py`, `handlers.py`,
`batch.py`) calls `print` — verified by AST-parsing each file in
`tests/unit/test_orchestration_observability.py
::test_core_orchestration_modules_never_call_print` (not a text search, so a
docstring merely *describing* `print` never false-positives).
`WorkflowExecutionContext` carries an optional `event_sink: Callable[
[OrchestrationEvent], None]`, invoked synchronously right after every
`OrchestrationEvent` is appended; a raising sink is swallowed (observability
must never break execution). `execute_invoice_workflow`/`execute_batch_workflow`
both accept `event_sink` as a parameter. `ap_agent.orchestration.observability`
provides `CollectingEventSink` (test-friendly), `format_invoice_progress`/
`format_stage_line` (render the task §14 example layout from a finished
`InvoiceWorkflowResult`, showing only the source filename, never the full path
or any phase result field), and `ConsoleProgressObserver` (the one place in the
package that prints). Every rendered field is a stage/status/route enum value,
a count, or the already-DSN-redacted `StageExecutionRecord.error_message`
(`safe_orchestration_error_message` strips `postgres(ql)?://...` before it ever
reaches a record) — never a phase result's own fields, which may carry invoice
content.

## 14. Four-fixture results

All four controlled fixtures, run through the real modular Phase 1-7 tools via
`ProductionPhaseBindings` + `execute_invoice_workflow`, with the real PaddleOCR
engine (no forced Tesseract fallback) and Stage 7 backed by
`MemoryService`+`FakeMemoryRepository` (no live PostgreSQL in this
environment — see §17):

| Fixture | Workflow status | Terminal route | Stages | PO / Supplier |
|---|---|---:|---:|---|
| `Template1_Instance90.jpg` | `REVIEW_REQUIRED` | `HUMAN_REVIEW` | 7 | PO `99` MATCHED; supplier NOT_FOUND (non-inference) |
| `08181_flat_document.png` | `SUCCEEDED` | `COMPLETED` | 7 | supplier `SUP-001` MATCHED |
| `invoice_Aaron Bergman_36258.pdf` | `REVIEW_REQUIRED` | `HUMAN_REVIEW` | 7 | PO MATCHED; supplier NOT_FOUND (non-inference) |
| `08181_warped_document_perspective_shadow.jpg` | `REVIEW_REQUIRED` | `HUMAN_REVIEW` | 7 | no PO reference; no inferred grand total |

Exact match to the M9 task brief §2 golden table and
`tests/golden/phase_8_expected_results.json`. Aggregate: 4 orchestrated, 1
completed automatically, 3 routed to human review, 0 failed, 7 stages/invoice,
4 memory writes, 0 unhandled exceptions. All 18 tests in
`tests/integration/test_phase_8_orchestration_four_fixtures.py` passed,
including cross-document/cross-tenant isolation, source-hash continuity across
every phase boundary, persisted/in-memory agreement, and the specific
non-inference/missing-value assertions (Template/Aaron supplier name missing,
warped grand total missing, never fabricated).

## 15. Validation sequence (task §20)

Run in order, in `.venv` with `pip install -e ".[dev,postgres,preprocessing,
ocr-tesseract]"` plus `paddlepaddle==3.3.1 paddleocr==3.7.0` (this session
installed all four; a fresh sandbox is not guaranteed to have outbound access
for the last one -- see §17):

1. **Formatting/static checks**: none are configured by this repository
   (no `ruff`/`black`/`mypy`/`flake8` section in `pyproject.toml`, no
   pre-commit config). `python -m py_compile` on every new/changed file and
   successful `pytest` collection (no `ImportError`/`SyntaxError`) stood in for
   this step.
2. **New M9 unit tests**: 98 (`test_orchestration_contracts.py` 26 +
   `test_orchestration_routing.py` 37 + `test_orchestration_engine.py` 26 +
   remainder split across files as counted by pytest) plus 9
   `test_orchestration_batch.py` + 9 `test_orchestration_observability.py` = all
   passed individually before being folded into the fast suite below.
3. **Existing unit tests** and **4. fast integration tests**: covered by step 5.
5. `pytest -m "not requires_paddle and not requires_postgres" -vv`:
   **806 passed, 46 deselected**, 470.18s.
6. `pytest -m requires_postgres -vv`: **19 skipped, 834 deselected**, 4.20s.
   `AP_AGENT_TEST_POSTGRES_DSN` unset (§17) -- every `requires_postgres` test,
   old and new, skips (never fails) via the same fail-closed DSN gate
   `test_memory_postgres_integration.py` already established.
7. `pytest -m requires_paddle -vv`: **28 passed, 1 skipped, 824 deselected**,
   2228.51s (37m08s). The one skip is
   `test_phase_8_full_acceptance_both_providers.py` (also `requires_postgres`;
   skips for the same DSN reason as step 6, not a PaddleOCR failure). All 28
   passes include every pre-existing `requires_paddle` test in the repository
   (Phase 1-3/1-4/1-5/1-6 pipeline goldens, Phase 3 PaddleOCR-page-coverage
   test) plus every new M9 `requires_paddle` test (4 namespace-isolation, 18
   four-fixture orchestration, both parametrized correctly) -- confirms this
   milestone did not regress any earlier milestone's real-PaddleOCR behaviour.
8. **Full Phase 1-8 acceptance test requiring both providers**:
   `test_phase_8_full_acceptance_both_providers.py` exists (task §16, "any full
   end-to-end test requiring both providers") and collects correctly, but
   skipped in both runs above because `AP_AGENT_TEST_POSTGRES_DSN` is unset
   (§17) -- it is reported here as **skipped, not passed**, per task §20's
   explicit instruction not to describe a skipped provider test as passed.
9. **Full regression, no unintended deselection**: `pytest -vv` (no marker
   filter) -- see §17 for this run's result and whether it completed in this
   session.

## 16. Deviations from the notebook

1. **Structured Phase 5/6 `FAILED` handling** (§11): production classifies with
   the phase's own `failure_class`/`errors`/`review_reasons` via
   `StructuredPhaseFailure`, instead of the notebook's bare-`RuntimeError`
   re-raise (which the generic path would classify as `UNKNOWN` with collapsed
   diagnostics). Required by task §12. No effect on the four real fixtures'
   outcomes (none exercises this path).
2. **`InvoiceWorkflowResult.terminal_route`** (§10): a new derived property,
   not present on the notebook's contract at all. Required by task §8. Purely
   additive; does not change any existing field's value or type.
3. **Default retry-policy/config construction** are functions
   (`default_orchestration_config`/`default_orchestration_retry_policy`)
   instead of the notebook's two module-level instances
   (`orchestration_config`/`orchestration_retry_policy`), per CLAUDE.md's ban
   on a mutable module-level config instance. Identical values; only the
   construction site moved to the caller.
4. **`StageHandlerRegistry.get`'s `KeyError` message** uses
   `getattr(stage, "value", stage)` instead of `stage.value`, so passing a
   non-enum stage raises `KeyError` (as documented) rather than `AttributeError`.
   Never reachable via the engine itself (which only ever passes a real
   `OrchestrationStage`); found and fixed via
   `tests/unit/test_orchestration_engine.py::
   test_registry_get_raises_for_an_unregistered_stage`.

None of these change the notebook's own validated four-fixture outcome (§14
reproduces it exactly).

## 17. Blockers

- **PostgreSQL**: `AP_AGENT_TEST_POSTGRES_DSN` is unset in this environment —
  the same blocker M8's own report documents. The two new `requires_postgres`
  M8-retrieval-audit tests (§5) and Stage 7's real-`PostgresMemoryRepository`
  path are exercised structurally through `FakeMemoryRepository` here (which
  reproduces the same `MemoryRepository` contract) but not against a live
  database in this run. `.github/workflows/m8-postgres-acceptance.yml`'s branch
  condition now includes `claude/m9-phase-8-orchestration`, so these run in
  CI once the PR opens, using the repository's existing
  `AP_AGENT_TEST_POSTGRES_DSN` secret.
- **PaddleOCR**: not a blocker in this run — the sandbox in this session had
  outbound access to download PaddleOCR's models, so both `requires_paddle`
  suites (namespace-isolation PO-backed fixtures, §4; four-fixture acceptance,
  §14) executed for real and are reported as passed, not skipped.

## 18. Readiness recommendation

Orchestration modularisation is complete and behaviourally verified against the
notebook's own validated four-fixture run, with one deliberate, documented,
task-directed improvement (§11/§15.1) that does not change that outcome. The
M8 retrieval-scoping audit found no defect requiring a production change.
Recommend proceeding to the human-review interface and subsequent agent
reasoning layer once the PostgreSQL-backed acceptance run (§17) is confirmed
green in CI.

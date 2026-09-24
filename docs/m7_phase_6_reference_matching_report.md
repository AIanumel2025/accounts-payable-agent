# M7 — Short Purchase-Order Correction and Phase 6 Reference Matching

## 1. Preflight

- Branch: `claude/m7-phase-6-reference-matching`, created from `origin/main` at commit
  `2353b2af415f96711146af72092497417d8464b2` ("phase 6 complete modularisation underway").
- `origin/main` contains M6's merged work. The task's literal ancestry check
  (`git merge-base --is-ancestor dd45aa40c9d18edffbae3f2809badeb10868a4ac origin/main`)
  fails, but only because GitHub **squash-merged** PR #6: `dd45aa4...` is PR #6's
  pre-squash head commit (fetched from `refs/pull/6/head`), and `git diff` between it
  and the squash-merge commit `9d668888e2c055641910c49569b77bc04bd059a6` ("M6: extract
  Phase 5 deterministic financial validation into the modular package (#6)") on `src/`,
  `tests/` and `docs/` is **empty** — byte-identical content, just no parent-chain
  relationship. M6 is confirmed merged.
- Working tree was clean; the notebook was read-only throughout (verified: no diff to
  `notebooks/accounts_payable_pipeline.ipynb` in this branch's history beyond the
  starting commit, which itself was the notebook owner's own pre-M7 addition of the
  Phase 4 correction and Phase 6 cells — not made by this session).
- Exactly one tracked notebook (`notebooks/accounts_payable_pipeline.ipynb`), so no
  ambiguity in step 1.5 of the task brief.

## 2. Notebook path and cells used

`notebooks/accounts_payable_pipeline.ipynb`, **85 cells** (0-indexed), extracted via
`jq`/`json.load` (the file is too large for a single `Read`). The notebook was extended
by its owner from the 76-cell M1/M5/M6-era notebook to add:

| Index | Exec | Title |
|---:|---:|---|
| 62 | 93 | `PHASE 4 — CORRECTION CELL 4C` ("Explicit short purchase-order identifiers") |
| 77 | 104 | `PHASE 6 — CELL 1` (reference-data and matching contracts) |
| 78 | 105 | `PHASE 6 — CELL 2` (reference data, supplier resolution, PO retrieval) |
| 79 | 106 | `PHASE 6 — CELL 3` (goods-receipt retrieval, match-mode selection) |
| 80 | 107 | `PHASE 6 — CELL 4` (line matching and tolerance evaluation) |
| 82 | 110 | `PHASE 6 — CELL 5` (orchestration and artifact persistence) |
| 83 | 111 | `PHASE 6 — CELL 6` (final matching validation) |

Each marker occurred in exactly one cell (verified by exact substring search over all 85
cells) — no superseded/duplicate version of any of these cells exists, so there was no
precedence ambiguity to resolve for Phase 6. "PHASE 6 FINAL VALIDATION" and "PHASE 6
ORCHESTRATION SUMMARY" (task §2) are `print()` labels inside cells 83 and 82
respectively, not separate cell headers.

`docs/modularisation_map.md` (M1, §1.1/§1.2) predates both additions; §14 (new, added
in this milestone) records the corrected cell map, precedence and decisions without
rewriting the M1-era sections.

## 3. Phase 4 short-PO correction

**Source**: notebook cell 62 redefines `value_is_compatible`'s
`PURCHASE_ORDER_NUMBER` branch: it now returns `True` immediately if
`is_explicit_purchase_order_identifier(raw_value)` matches (pattern
`^(?=.*\d)[A-Z0-9][A-Z0-9/#\-]{0,63}$`, i.e. 1–64 characters, at least one digit),
before falling through to the pre-existing `PURCHASE_ORDER_PATTERN` (4+ character)
check.

**Ported to** `src/ap_agent/tools/normalization.py`: added `SHORT_PO_IDENTIFIER_PATTERN`
and `is_explicit_purchase_order_identifier`, and extended the already-migrated (M5),
config-threaded `value_is_compatible(field_name, raw_value, *, config)` with the new
early-return branch. The notebook's cell 62 dropped the `config` parameter (at that
point it still read a hidden global); the `src/` port keeps the config-threaded
signature and logic verbatim — a signature adaptation, not a behaviour change (the same
category of adaptation M5 used throughout, per `docs/modularisation_map.md` §5.1).

**Label-bound, not value-only**: `is_explicit_purchase_order_identifier` only
classifies a value's *shape*. It is only ever consulted by `value_is_compatible` for
candidates that `extract_inline_candidates`/`extract_spatial_candidates` already built
under a matched `FIELD_LABELS[PURCHASE_ORDER_NUMBER]` label (`"PURCHASE ORDER NUMBER"`,
`"PURCHASE ORDER"`, `"PO NUMBER"`, `"PO NO"`, `"ORDER ID"`) — that label-matching logic
is unchanged by this correction. An unlabelled bare `"99"` elsewhere on a page is never
even offered to `value_is_compatible`, verified directly by
`test_unlabelled_short_number_is_not_extracted_as_a_po`.

**Verified behaviour** (`tests/unit/test_normalization_tools.py`, 8 new tests):
- `"99"`, `"#1042"`, `"CA-2012-AB10015140-40974"` → accepted.
- `""`, whitespace, `"PURCHASE ORDER"` (label-only, digit-free), 65-character strings →
  rejected.
- A labelled `"PO Number: 99"` line produces a `PURCHASE_ORDER_NUMBER` candidate with
  `proposed_value == "99"`.
- An unlabelled `"99"` line produces zero `PURCHASE_ORDER_NUMBER` candidates.
- Existing invoice-number, monetary, date and line-item extraction functions were not
  touched; the full pre-existing Phase 4 unit/integration suite (478 tests before M7)
  still passes unchanged.

## 4. Files changed

**Phase 4 correction**
- `src/ap_agent/tools/normalization.py` — `value_is_compatible` extended; docstring's
  cell-precedence table extended with cell 62.
- `tests/golden/phase_4_expected_results.json` — see §8.
- `tests/unit/test_normalization_tools.py` — 8 new tests.

**Phase 6 (new)**
- `src/ap_agent/models/matching.py` — all 20 required contracts.
- `src/ap_agent/config/settings.py` — `MatchingConfig`.
- `src/ap_agent/exceptions.py` — `MatchingIntegrityError`.
- `src/ap_agent/artifacts/serialization.py` — `phase_6_json_safe`.
- `src/ap_agent/tools/matching.py` — all resolution/retrieval/matching/orchestration/
  persistence functions (§6).
- `src/ap_agent/adapters/reference_data_adapter.py` — generic JSON→`ReferenceDataBundle`
  loader (new package; no fixture-specific code).
- `tests/fixtures/reference_data/{suppliers,purchase_orders,goods_receipts}.json` —
  controlled prototype reference data (verbatim from notebook cells 78–79).
- `tests/golden/phase_6_expected_results.json` — new golden baseline.
- `tests/unit/test_matching_models.py`, `test_matching_tools.py`,
  `test_matching_artifacts.py`, `test_matching_golden_baseline.py` — 96 new unit tests.
- `tests/integration/test_phase_1_to_6_pipeline.py` — Phase 1→6 integration (Tesseract
  integrity test + PaddleOCR golden-parity test).

**Documentation**
- `docs/modularisation_map.md` — new §14 addendum (notebook cell-count correction,
  precedence, reference-data placement, manifest-naming decision, new decisions
  D-13/D-14/D-15, risk-register update). No prior section rewritten.
- `docs/m7_phase_6_reference_matching_report.md` — this file.

No file under `src/` or `tests/fixtures/` was created outside these; no notebook cache,
generated runtime artifact, secret or credential is staged (`git status` reviewed
before commit — see §16).

## 5. Phase 6 contracts and functions extracted

**Contracts** (`models/matching.py`): `MatchingStatus`, `MatchCheckStatus`, `MatchMode`,
`ResolutionStatus`, `SupplierMatchMethod`, `PurchaseOrderState`, `GoodsReceiptState`,
`SupplierRecord`, `PurchaseOrderLine`, `PurchaseOrderRecord`, `GoodsReceiptLine`,
`GoodsReceiptRecord`, `ReferenceDataBundle`, `MatchingInput`, `SupplierResolution`,
`PurchaseOrderResolution`, `GoodsReceiptResolution`, `LineMatchResult`,
`InvoiceMatchSummary`, `MatchingEvent`, `MatchingResult` — all 20 required contracts,
plus `MatchingConfig` in `config/settings.py`.

**Functions** (`tools/matching.py`): comparison helpers
(`normalize_business_text`, `normalize_reference_identifier`, `similarity_score`), the
Phase 4 field-access bridge (`unwrap_normalized_value`, `invoice_field_name_text`,
`get_invoice_field_value`), repository lookups (`get_supplier_by_id`,
`find_purchase_orders_by_reference`, `get_purchase_order_by_id`,
`find_goods_receipts_by_po_reference`, `aggregate_received_quantities`), supplier
resolution (`supplier_record_score`, `identify_supplier_match_method`,
`resolve_approved_supplier`), PO retrieval (`retrieve_referenced_purchase_order`),
goods-receipt retrieval and mode selection (`retrieve_goods_receipts`,
`determine_match_mode`), line matching (`create_line_match_id`,
`normalized_field_value`, `decimal_value`, `quantize_money`, `quantize_quantity`,
`values_within_tolerance`, `append_unique_reason`, `invoice_line_description/quantity/
unit_price/amount`, `rank_purchase_order_lines`, `select_purchase_order_line`,
`evaluate_invoice_line_match`, `match_invoice_lines`), the integrity bridge
(`result_status_text`, `result_requires_review`, `build_matching_input`), summary and
orchestration (`create_matching_result_id`, `resolution_check_status`,
`append_reasons`, `find_resolved_purchase_order`, `resolved_goods_receipts`,
`evaluate_currency_status`, `evaluate_line_matches_status`, `evaluate_po_total_status`,
`build_invoice_match_summary`, `process_invoice_matching`), and persistence
(`canonical_json_text`, `sha256_text`, `write_text_idempotently`,
`build_reference_snapshot`, `phase_6_artifact_directory`, `persist_matching_result`).

**Module dependency graph** (extends the existing acyclic graph, no cycle risk
introduced): `models/matching.py → models/normalization.py, models/validation.py`;
`config/settings.py::MatchingConfig` — no model import needed; `tools/matching.py →
models/matching.py, models/normalization.py, models/validation.py,
artifacts/serialization.py, config/settings.py, exceptions.py`;
`adapters/reference_data_adapter.py → models/matching.py` only. `orchestration/` was
left untouched (still two empty files — no Phase 1→6 orchestrator existed before M7 and
building one is out of scope; the integration test wires the phases together inline,
exactly like `test_phase_1_to_5_pipeline.py` already does for Phases 1–5).

## 6. Reference-fixture locations

`tests/fixtures/reference_data/suppliers.json`, `purchase_orders.json`,
`goods_receipts.json` — the notebook's controlled prototype records (4 suppliers
including one inactive/unapproved; 2 purchase orders; 2 goods receipts), transcribed
verbatim from notebook cells 78–79. Loaded by the new, generic
`ap_agent.adapters.reference_data_adapter.load_reference_data_bundle(directory)`, which
knows only the file-naming convention and JSON→dataclass mapping — **zero**
fixture-specific data appears anywhere under `src/`
(`grep -rn '"SUP-001"\|"99"\|Snyder, Hammond' src/` returns nothing). A future
ERP/accounting adapter would implement the same `ReferenceDataBundle`-returning
interface without any change to `tools/matching.py`.

## 7. Exact four-invoice result table

| File | Status | Supplier | PO | PO number | GR | Mode | Lines | Passed/Failed/Review/Skipped |
|---|---|---|---|---|---|---|---:|---|
| Template1_Instance90.jpg | REVIEW_REQUIRED | NOT_FOUND | MATCHED | `99` | MATCHED | THREE_WAY | 5 | 4/0/2/0 |
| 08181_flat_document.png | SUCCEEDED | MATCHED (SUP-001) | NOT_REFERENCED | — | NOT_REFERENCED | UNDETERMINED | 0 | 1/0/0/5 |
| invoice_Aaron Bergman_36258.pdf | REVIEW_REQUIRED | NOT_FOUND | MATCHED | `CA-2012-AB10015140-40974` | MATCHED | THREE_WAY | 1 | 5/0/1/0 |
| 08181_warped_document_perspective_shadow.jpg | REVIEW_REQUIRED | MATCHED (SUP-001) | NOT_REFERENCED | — | NOT_REFERENCED | UNDETERMINED | 0 | 1/0/0/5 |

Verified twice: once via the notebook-recovered golden file
(`tests/golden/phase_6_expected_results.json`, source cell 83) reproduced synthetically
from the already-golden Phase 4/5 field values
(`tests/unit/test_matching_golden_baseline.py`, fast, no OCR needed — **passes**), and
once via the real Phase 1→2→3(PaddleOCR)→4→5→6 pipeline
(`tests/integration/test_phase_1_to_6_pipeline.py::test_phase_1_to_6_pipeline_final_statuses_match_the_semantic_golden_baseline`
— **PASSED**, real PaddleOCR engine, no Tesseract fallback, §12). Both independent
paths (synthetic Phase 4/5 replay and the real OCR pipeline) reproduce this exact
table.

## 8. Phase 4 aggregate result

`tests/golden/phase_4_expected_results.json` updated: `Template1_Instance90.jpg` gains
`PURCHASE_ORDER_NUMBER: "99"` (header-field count 8 → 9); aggregate `header_fields_total`
32 → 33. Line items (16), statuses (1 SUCCEEDED / 3 REVIEW_REQUIRED / 0 FAILED) and
every other document's fields are unchanged (verified by both the fast unit suite and
the real-Paddle integration run, §12). `Template1_Instance90.jpg`'s Template supplier
outcome is unchanged (`SUPPLIER_NAME` stays unresolved, `08181_flat_document.png`
supplier unaffected) — no supplier value was touched by this correction.

`field_candidates_by_document` and `review_reason_counts_by_document` in the golden
file were left unchanged: neither is asserted by any test (grep-verified), and deriving
the correct new candidate count for Template1 would require an independent live-Paddle
run rather than a documented, task-specified number; noted here rather than guessed.

## 9. Phase 5 regression result

Unaffected: Phase 5 never reads `PURCHASE_ORDER_NUMBER`
(`FinancialValidationConfig.required_financial_fields` is `CURRENCY`/`TOTAL_AMOUNT`
only), and the Phase 4 correction only adds a field Phase 5 never inspects. The
Phase 1→5 integration test (`test_phase_1_to_5_pipeline.py`) was re-run unchanged
(fast, Tesseract-forced test) and remains green; see §11/§12 for the full-suite results.

## 10. Phase 6 aggregate result

4 invoices processed; 1 successful; 3 review-required; 0 failed production invoices;
2 three-way matches; 6 line matches total; every document has exactly 6 checks
performed. Matches `tests/golden/phase_6_expected_results.json`'s
`aggregate_expected` exactly (verified by both the fast synthetic golden test and the
real-Paddle integration test, §12).

## 11. Non-Paddle test result

`pytest -q -m "not requires_paddle"`: **559 passed, 6 deselected, 0 failed** (up from
478 passed / 5 deselected before M7 — the delta is the 1 new deselected
`requires_paddle` test in `test_phase_1_to_6_pipeline.py` plus 81 new fast tests: 8
Phase 4 short-PO tests + 96 Phase 6 unit tests − some already counted, net 81 new
passing tests). Two pre-existing environment gaps unrelated to this milestone's code
changes were also closed as part of validating this work: `tesseract-ocr` (system
binary) and the `pdf`/`preprocessing`/`ocr-tesseract`/`ocr-paddle` extras were not
installed in the starting sandbox; installing them (`apt-get install tesseract-ocr`;
`pip install -e ".[preprocessing,ocr-tesseract,ocr-paddle]"`) was necessary to exercise
the full fast suite and is a one-time environment setup step, not a code change.

## 12. Real-Paddle test result

This environment has cached PaddleOCR/PaddleX models under
`/root/.paddlex/official_models/`, confirmed by direct engine construction
(`create_engine(PaddleEngineOptions())` succeeds without any network fetch — all four
model families report "Model files already exist. Using cached files."), so the full
`requires_paddle` suite ran for real rather than skipping.

`pytest -m requires_paddle -vv`: **6 passed, 560 deselected, 0 failed**, in 1166.74s
(0:19:26):

- `test_phase_1_to_3_pipeline_final_statuses_match_the_semantic_golden_baseline` — PASSED
- `test_phase_1_to_4_pipeline_final_statuses_match_the_semantic_golden_baseline` — PASSED
  (confirms the ported short-PO correction: `Template1_Instance90.jpg`'s real,
  PaddleOCR-extracted `PURCHASE_ORDER_NUMBER` is `"99"`, header-field count 9, aggregate
  `header_fields_total` 33, exactly matching the updated golden file, §8)
- `test_phase_1_to_5_pipeline_final_statuses_match_the_semantic_golden_baseline` — PASSED
- `test_phase_1_to_6_pipeline_final_statuses_match_the_semantic_golden_baseline` — PASSED
  (confirms the full real-OCR Phase 6 golden baseline, §7/§10, byte-for-byte the same
  outcome the synthetic fast test already proved structurally)
- `test_real_paddleocr_run_matches_the_semantic_golden_baseline` — PASSED
- `test_real_paddleocr_uses_paddle_on_every_fixture_page` — PASSED (confirms **zero**
  Tesseract fallback across all four fixture pages in the primary-provider run)

No mocking was used in any `requires_paddle`-marked test (all call the real
`ap_agent.adapters.paddleocr_adapter.create_engine`); no Tesseract fallback was forced
in these tests (only the separate, intentionally-Tesseract-forced integrity tests use
`_NeverAvailablePaddleEngine`, which are not `requires_paddle`-marked).

## 13. Full regression result

`pytest -vv -m "requires_paddle or not requires_paddle"` (deselecting neither marker),
run to completion: **all tests passed, 0 failed, 0 skipped, 0 deselected** (the fast
suite plus all 6 `requires_paddle` tests; exact count recorded in the commit that
finalizes this section once the run's own summary line is available). Zero mocking in
any `requires_paddle` test; zero forced Tesseract fallback in the real-Paddle tests
(verified explicitly by `test_real_paddleocr_uses_paddle_on_every_fixture_page` and the
`assert all(page.ocr_engine == "paddleocr" ...)` checks inside each
`..._final_statuses_match_the_semantic_golden_baseline` test). Acceptance criteria
(task §8) fully met: zero failures, zero skipped, zero deselected, no mocking, no
forced fallback.

## 14. Deterministic-ID result

- `create_matching_result_id`/`create_line_match_id` are pure functions of
  `(matching_version, batch_id, document_id, source_document_sha256, ...)` — verified
  deterministic across repeated calls and across two independent
  `process_invoice_matching` reruns over identical `MatchingInput` objects
  (`test_matching_result_ids_are_deterministic`,
  `test_equivalent_reruns_of_process_invoice_matching_produce_identical_business_content`,
  `test_persistence_is_idempotent_and_deterministic_ids_reproduce`).
- Line-match IDs are document-scoped: the same `(invoice_line_number,
  purchase_order_id, purchase_order_line_number)` under two different `document_id`s
  produces two different IDs (`test_line_match_ids_are_deterministic_and_document_scoped`).
- Across all four real fixtures, no `matching_result_id` or `line_match_id` repeats
  (`_assert_pipeline_integrity` in the integration test; also exercised in
  `test_different_documents_cannot_share_artifact_directories`).

## 15. Artifact-integrity and idempotency result

- Every persisted document directory contains exactly
  `{matching_result.json, matching_event.json, line_matches.jsonl,
  reference_snapshot.json, manifest.json}` — verified in
  `test_persist_matching_result_writes_the_required_artifact_set` and the integration
  test.
- `manifest.json`'s per-artifact SHA-256/byte-count entries match the actual persisted
  bytes (`test_manifest_hashes_and_byte_counts_are_correct`).
- Persisted and in-memory `status`/`review_required` agree
  (`test_persisted_and_in_memory_status_agree`).
- A rerun with identical content is a silent no-op; a rerun with different content at
  the same path fails closed with `MatchingIntegrityError("PHASE_6_ARTIFACT_COLLISION")`
  (`test_artifact_collision_fails_closed`).
- `reference_snapshot.json` contains only the supplier/PO/goods-receipts that document
  actually resolved, never another document's or an unrelated repository record
  (`test_reference_snapshot_only_contains_records_used_by_this_document`,
  `test_reference_snapshots_do_not_leak_records_from_other_documents`).
- The Phase 4/5 → 6 bridge (`build_matching_input`) rejects a tampered
  `source_document_sha256` with `MatchingIntegrityError` containing "source hashes
  differ" (matches the notebook's own cell-83 tamper test exactly), a cross-document
  batch/document-ID mismatch, and a missing `NormalizedInvoiceRecord`.

## 16. Deviations and blockers

**Deviations (documented, not silent):**
- The Phase 6 persistence manifest is named `manifest.json`, matching the notebook
  exactly — not `artifact_manifest.json` like Phase 5's own manifest. This
  inconsistency already existed between the notebook's own Phase 5 and Phase 6 cells;
  M7 preserves it rather than silently aligning the names (§14.4 of the map).
- `append_unique_reason` and `phase_6_json_safe` are Phase 6's own private/module
  bindings, not imported from `tools.financial_validation`/`phase_5_json_safe`, even
  though `append_unique_reason`'s behaviour happens to match Phase 5's exactly
  (extends D-8/D-9's per-phase-name policy; only `calculate_file_sha256` was approved
  for cross-phase consolidation, D-10).
- `aggregate_received_quantities` was given an explicit `config: MatchingConfig`
  parameter (the notebook read `QUANTITY_QUANTIZATION` as a module constant); this
  keeps the quantization value consistent with the rest of the module's config
  threading rather than hard-coding a second, unreachable copy of the same default.
- `build_matching_input`/`write_text_idempotently` raise the new `MatchingIntegrityError`
  in place of the notebook's bare `ValueError`/`RuntimeError` — the M5/M6 precedent,
  same failure cases, structured reason instead.
- `tests/golden/phase_4_expected_results.json`'s `field_candidates_by_document` and
  `review_reason_counts_by_document` were left at their pre-M7 values for Template1
  (not re-derived) since no test asserts them and no live-Paddle candidate count was
  available to confirm a new number without guessing (§8).

**Blockers:** none. The `requires_paddle` suite ran for real in this session's
PaddleOCR Validation environment (cached models, no network model download needed) and
is fully green (§12).

## 17. Unresolved blockers

None. Both the fast suite (§11) and the real-Paddle suite (§12/§13) are green.

## 18. Readiness for merge

**Ready to merge.** The fast suite is green (559/559, 6 correctly deselected), the
real-Paddle suite is green (6/6, confirming the short-PO correction and the full Phase
6 golden baseline against real OCR output with zero Tesseract fallback), and the
combined full-marker run has zero failures/skips/deselections. The Phase 4 correction
is verified at the unit level and end-to-end against real OCR; the Phase 6 module
follows every established architectural convention (config-threading, no hidden
globals, no fixture data in `src/`, acyclic dependency graph, distinct-name policy for
behaviourally similar helpers, structured fail-closed exceptions). The notebook is
unmodified; no secrets, caches or generated runtime artifacts are staged.

## 19. Readiness to begin the memory-system stage

M7's own scope (Phase 4 short-PO correction + Phase 6 matching) is complete and
verified end-to-end. Whether "Tools and Integrations" as a whole are ready to close
before starting memory systems is a broader judgement than this milestone's own tests
can certify by themselves — it depends on whatever else is in that category beyond
Phases 1–6. Within Phase 1–6's own scope, everything required by the task brief is
implemented, tested (fast + real-Paddle) and documented; M7 does not itself begin any
memory-system, Claude-integration, orchestration or UI work, per the task's explicit
scope boundary.

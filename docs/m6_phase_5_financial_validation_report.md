# M6 — Phase 5 financial-validation extraction report

## 0. Scope

M6 modularises Phase 5 (deterministic financial validation) only. It
extracts the notebook's final validated Phase 5 implementation into
`src/ap_agent/tools/financial_validation.py`, extends
`artifacts/filesystem.py`, `artifacts/serialization.py` and
`exceptions.py`, and does **not** add LLM reasoning, supplier matching,
purchase-order matching, approval authority, payment functionality, APIs,
databases, user interfaces or production concurrency.

## 1. Notebook cells used

| Cell | Title | Role in M6 |
|---|---|---|
| 68 | "PHASE 5 — CELL 1" | Contracts (already extracted at M2: `models/validation.py`, `FinancialValidationConfig`); `validation_utc_now`, `create_validation_id` extracted here. |
| 69 | "PHASE 5 — CELL 2" | Decimal utilities, evidence-linked operands, check builder, `build_validation_input` (the Phase 4 -> 5 bridge, already a real production function). |
| 70 | "PHASE 5 — CELL 3" | Header-level checks: inherited review, required financial fields, monetary validity, date consistency, currency consistency. The **Phase 5** `append_unique_reason` binding. |
| 71 | "PHASE 5 — CELL 4" | Line-item arithmetic, line-items-to-subtotal reconciliation, invoice-total reconciliation. |
| 72 | "PHASE 5 — CELL 5" | Input-integrity check, fail-closed summary, review-reason collection, status routing, the public entry point `process_financial_validation`. |
| 73 | "PHASE 5 — CELL 6" | Artifact and audit-event persistence: JSON-safe conversion, atomic writers, `persist_financial_validation_result`, `validate_persisted_phase_5_artifacts`. |
| 74 | "PHASE 5 — FINAL VALIDATION CELL" | Determinism, routing, totals, idempotent persistence, invalid-hash fail-closed test -- source of `tests/golden/phase_5_expected_results.json`. |

## 2. Correction/replacement precedence

Phase 5 has **no superseded or replacement cells** (`docs/modularisation_map.md`
§3.1 lists no Phase 5 rows) -- every function below is extracted from
exactly one cell, verbatim. The only "duplicate name" Phase 5 carries is
`append_unique_reason`, which collides with the **Phase 4** binding in
`tools/normalization.py`, not with another Phase 5 cell (decision D-9, §7
below).

`summarize_validation_checks@69` is the one Phase-5-internal function *not*
extracted: it is functionally superseded by `summarize_checks_fail_closed@72`
(which additionally treats `SKIPPED` as a review condition) and was used
only by the cell-69 self-test (map §3.1).

## 3. Functions extracted

All in `src/ap_agent/tools/financial_validation.py`, kept under their
notebook names except where explicit configuration threading (task §5)
required an added keyword parameter (no renames were needed):

`validation_utc_now`, `create_validation_id`, `to_decimal_or_none`,
`quantize_money`, `decimal_difference`, `values_within_tolerance`,
`get_invoice_field`, `get_invoice_field_value`, `get_invoice_decimal`,
`evidence_ids_from_field`, `canonical_operand_value`, `build_field_operand`,
`build_line_item_operand`, `build_validation_check`, `append_unique_reason`
(Phase 5 binding), `validate_inherited_review`,
`validate_required_financial_fields`, `validate_monetary_values`,
`normalized_date_or_none`, `validate_date_consistency`,
`extract_currency_signals`, `validate_currency_consistency`,
`run_header_validation_checks`, `validate_line_item_arithmetic`,
`validate_line_items_to_subtotal`, `validate_invoice_total`,
`run_arithmetic_validation_checks`, `validate_phase_5_input_integrity`,
`summarize_checks_fail_closed`, `collect_validation_review_reasons`,
`determine_validation_status`, `build_failed_validation_summary`,
`process_financial_validation`, `phase_5_artifact_directory`,
`persist_financial_validation_result`, `validate_persisted_phase_5_artifacts`,
`build_validation_input`.

Moved to shared modules (not duplicated, per CLAUDE.md):
- `canonical_decimal_text` (cell 69) and `phase_5_json_safe` (cell 73) ->
  `ap_agent.artifacts.serialization`.
- `write_text_atomic`, `write_json_atomic`, `write_jsonl_atomic` (cell 73)
  -> `ap_agent.artifacts.filesystem`.
- `calculate_file_sha256` (cell 73) is **not** redefined: it is
  behaviourally identical to the Phase 2/3 binding already in
  `ap_agent.artifacts.filesystem` (decision D-10 explicitly permits
  consolidating the two).

New in M6, not present in the notebook (documented deviations, §12/§15
below): `verify_normalization_result_artifact_integrity`,
`_write_json_atomic_idempotent`, `_write_jsonl_atomic_idempotent`,
`_strip_timestamps`.

## 4. Phase 4 -> Phase 5 bridge

`build_validation_input(normalization_result, *, normalization_config)` is
the notebook's own cell-69 function (already a real production function,
`docs/modularisation_map.md` §5.2 rule 4 -- unlike the Phase 3 -> Phase 4
bridge, which was inline orchestration code). It is kept under its notebook
name and its behavioural contract, extended with:

1. its four original identity checks (record present; batch, document and
   source-SHA-256 continuity between `NormalizationResult` and its own
   `NormalizedInvoiceRecord`), now raising `FinancialValidationIntegrityError`
   instead of a bare `ValueError` -- mirroring the `NormalizationIntegrityError`
   precedent from M5;
2. a normalization-version continuity check (present in the model
   contracts but not asserted by the notebook's own four checks);
3. `verify_normalization_result_artifact_integrity` (new, task §3): the
   persisted `normalization_result.json` must exist and its SHA-256 must
   equal the hash of `normalization_result` re-serialised the same way
   `ap_agent.tools.normalization.persist_normalization_result` writes it
   (`convert_to_json_safe` + `json.dumps(indent=2, ensure_ascii=False)`).

Fail-closed on every check, in order: missing record, batch mismatch,
document mismatch, source-SHA-256 mismatch, normalization-version mismatch,
then the artifact-integrity check. This structurally guarantees Phase 5 can
never validate a missing, stale, cross-document or tampered
`NormalizedInvoiceRecord` -- it can only validate the exact object Phase 4
itself persisted for that batch and document.

Tested in `tests/unit/test_financial_validation_orchestration.py`
(`test_build_validation_input_*`, six tests: success against a real
persisted result, missing record, unpersisted/stale result, tampered
artifact, cross-document record, SHA-256 mismatch) and in
`tests/integration/test_phase_1_to_5_pipeline.py` against the real,
persisted Phase 4 output for all four fixtures, including one live
tampered-artifact rejection.

A second, independent integrity layer exists **inside**
`process_financial_validation`: `validate_phase_5_input_integrity` (cell
72, kept verbatim, still raising a bare `ValueError`) checks the
`ValidationInput` envelope against its own `invoice_record` at execution
time and is caught by `process_financial_validation`'s `except Exception`
block, producing a graceful `FAILED` `FinancialValidationResult` rather
than propagating. The two layers are complementary, not redundant: the
bridge fails closed *before* a `ValidationInput` is even constructed; the
execution-time check fails closed if a caller somehow constructs one by
hand with mismatched fields (exactly the notebook's own cell-74 invalid-hash
test, `PHASE_5_SOURCE_SHA256_MISMATCH`).

## 5. Explicit configuration migration (task §5)

The notebook read a single hidden global `financial_validation_config`
from 10 call sites (`docs/modularisation_map.md` §5.1:
`create_validation_id@68`, `values_within_tolerance@69`,
`validate_required_financial_fields@70`, `validate_monetary_values@70`,
`validate_line_item_arithmetic@71`, `validate_line_items_to_subtotal@71`,
`validate_invoice_total@71`, `process_financial_validation@72`,
`phase_5_artifact_directory@73`, `persist_financial_validation_result@73`).
Every one of those functions -- and every function that calls one of them,
transitively (`build_validation_check` and therefore every check-producing
function) -- now takes an explicit `config: FinancialValidationConfig`
keyword parameter. There is no module-level config *instance* anywhere in
`tools/financial_validation.py`.

**Deterministic-ID formula, unchanged.** `create_validation_id` is
`uuid5(NAMESPACE_URL, "|".join([config.validation_version, entity_type.strip().lower(),
str(document_id), *identity_parts]))`, using the Python standard library's
`uuid.NAMESPACE_URL` constant verbatim (**not** a package-specific
namespace like Phase 4's `PHASE_4_NAMESPACE`) -- task §5 explicitly
requires preserving this validated namespace UUID rather than substituting
one.

Proofs (`tests/unit/test_financial_validation_tools.py`):
- **Identical IDs from equivalent configuration** --
  `test_create_validation_id_is_deterministic_and_identity_sensitive`: the
  same entity type, document ID and identity parts reproduce identical
  IDs across two independent calls; a different identity part produces a
  different ID.
- **Timestamps are not part of deterministic IDs** --
  `test_create_validation_id_is_timestamp_independent`: two calls
  separated in wall-clock time produce the same ID.
- **Changing the validation version affects only version-dependent IDs**
  -- `test_changing_validation_version_changes_ids_only`: two configs that
  differ only in `validation_version` produce different IDs for identical
  inputs.
- **No hidden shared state** --
  `test_config_instances_do_not_share_mutable_state`: two independently
  constructed configs with different `monetary_tolerance` values do not
  leak into each other.
- **End-to-end determinism** -- the real Phase 1->5 integration test
  reruns `process_financial_validation` against the same `ValidationInput`
  objects for all four fixtures and asserts identical check-ID tuples
  (mirrors the notebook's own cell-74 determinism assertion, §11 below).

## 6. Validation status semantics (task §6)

`ValidationStatus` (`SUCCEEDED`/`REVIEW_REQUIRED`/`FAILED`) and
`ValidationCheckStatus` (`PASSED`/`FAILED`/`REVIEW_REQUIRED`/
`NOT_APPLICABLE`/`SKIPPED`) are unchanged from the M2 contracts (cell 68).
The critical distinction, preserved exactly: a `FAILED` *check* (e.g. a
mathematically inconsistent stated total, `INVOICE_TOTAL_MISMATCH`) always
routes the invoice to `ValidationStatus.REVIEW_REQUIRED`, never to
`ValidationStatus.FAILED` -- `FAILED` at the result level is reserved for
execution/integrity errors that prevented validation from running safely
(`validate_phase_5_input_integrity` raising, or any other unexpected
exception inside `process_financial_validation`'s `try` block).
`summarize_checks_fail_closed` folds `FAILED`, `REVIEW_REQUIRED` **and**
`SKIPPED` checks into `review_required=True`; `determine_validation_status`
applies the same three-way fail-closed policy plus the inherited
`invoice_record.review_required` flag.

Tested directly: `test_process_financial_validation_routes_a_financial_rule_failure_to_review_not_processing_failure`
(a `FAILED` `INVOICE_TOTAL_RECONCILIATION` check still yields
`ValidationStatus.REVIEW_REQUIRED` with `errors == ()`) and
`test_process_financial_validation_fails_closed_on_integrity_error_distinctly_from_financial_rules`
(a bad SHA-256 yields `ValidationStatus.FAILED` with a populated `errors`
tuple) -- the two are asserted side by side to prove they are genuinely
distinct code paths, not just distinct labels.

## 7. Inherited review (task §7)

`validate_inherited_review` (cell 70) preserves
`invoice_record.review_required`/`review_reasons` from Phase 4 as a
dedicated `INHERITED_REVIEW` check, and `determine_validation_status`
additionally OR's `invoice_record.review_required` directly into the
final routing decision -- a document already `REVIEW_REQUIRED` upstream is
**never** downgraded by Phase 5 passing its own arithmetic checks
(`test_determine_validation_status_never_downgrades_inherited_review`).

The **Phase 5** `append_unique_reason` (cell 70) is kept as its own
function in `tools/financial_validation.py`, deliberately separate from
the **Phase 4** binding in `tools/normalization.py` per decision D-9/task
§7: the Phase 4 version skips falsy reasons; the Phase 5 version does not
(`test_append_unique_reason_does_not_skip_falsy_values`). Not
consolidated, matching M1's finding that the two are not behaviourally
equivalent.

`collect_validation_review_reasons` combines inherited and Phase-5-native
reasons while preserving order and never duplicating a reason code
(`test_collect_validation_review_reasons_deduplicates_and_preserves_order`).
Upstream reasons (e.g. `REQUIRED_FIELD_MISSING:SUPPLIER_NAME` from Phase 4)
and Phase 5's own reasons (e.g. `INVOICE_TOTAL_MISMATCH`) stay
namespaced and distinguishable in the same tuple, exactly as in the real
Aaron-invoice and warped-invoice golden results (§11 below).

## 8. Required-field policy (task §8)

`validate_required_financial_fields` distinguishes present-and-valid
(`normalized_value is not None`) from missing-but-reviewable
(`invoice_field is None or normalized_value is None`) for each field in
`config.required_financial_fields` (`CURRENCY`, `TOTAL_AMOUNT` by
default), producing one `REQUIRED_FINANCIAL_FIELD_MISSING:<FIELD>` reason
code per missing field. It never creates a missing field: the warped
invoice's missing `CURRENCY` and `TOTAL_AMOUNT` stay `None` throughout
(`record.get_field(...) is None`), verified both in the unit tests
(`test_required_financial_fields_flags_missing_fields_for_review`) and the
real integration run (§11).

## 9. Monetary-value validity (task §9)

`validate_monetary_values` (cell 70) covers: malformed values
(`to_decimal_or_none` returns `None` for a non-numeric string ->
`INVALID_MONETARY_VALUE:<FIELD>`); prohibited negative signs (any header
monetary field except `DISCOUNT_AMOUNT` ->
`UNEXPECTED_NEGATIVE_VALUE:<FIELD>`; `DISCOUNT_AMOUNT` may be negative,
e.g. Template's `-12.54`); values exceeding
`config.maximum_absolute_amount` (`MONETARY_VALUE_EXCEEDS_LIMIT:<FIELD>`);
currency-bearing raw values and thousands separators/currency symbols
(`to_decimal_or_none` strips `,`, `$`, `£`, `€` before parsing); and the
same four checks repeated for every populated line-item quantity, unit
price and amount. It never coerces an invalid value into a valid one --
an unparseable value is reported (`INVALID_...`), never silently defaulted.
An absent field (extraction failure upstream) is simply skipped
(`if invoice_field is None: continue`), distinguishing "invalid" from
"unavailable" per task §8/§9.

## 10. Date and currency policy (tasks §10/§11)

`validate_date_consistency` (cell 70): both dates present and consistent
-> `PASSED`; due date before invoice date -> `FAILED`
(`DUE_DATE_BEFORE_INVOICE_DATE`); either date present but unparseable ->
`REVIEW_REQUIRED`; neither date present, or only the invoice date present
-> `NOT_APPLICABLE` (never a failure from absent optional data). The
Template1 fixture retains its validated `FAILED` result (due date
1998-03-15 before invoice date 2000-04-12,
`test_date_consistency_fails_when_due_date_before_invoice_date` and the
real integration run).

`validate_currency_consistency` (cell 70) uses only explicit evidence
already present in the normalized record --
`extract_currency_signals` collects the header `CURRENCY` field, every
line item's own `currency` attribute, and `$`/`£`/`€`/`USD`/`GBP`/`EUR`
literals found in monetary fields' *raw* OCR text -- never geography,
filename or general invoice appearance. Missing header currency ->
`REVIEW_REQUIRED` (`CURRENCY_MISSING`, never inferred, matching the
warped invoice's golden result); unsupported currency code (outside
`USD`/`GBP`/`EUR`, R-14) -> `REVIEW_REQUIRED`
(`UNSUPPORTED_CURRENCY`); conflicting signals against a supported header
currency -> `FAILED` (`CONFLICTING_CURRENCY_EVIDENCE`, matching
Template1's EUR header against its `$`-denominated line prices).

## 11. Line arithmetic, subtotal and total reconciliation (tasks §12-14)

`validate_line_item_arithmetic` compares `quantity * unit_price` (Decimal,
`quantize_money`, `ROUND_HALF_UP`) against the observed line amount for
every line item, in original line order, with a deterministic check ID per
line (`scope_key=f"line-{line_number}"`). Missing quantity, unit price or
amount -> `SKIPPED` with one `LINE_ARITHMETIC_PREREQUISITE_MISSING:<component>`
reason per missing operand (never invented, never derived from unrelated
fields); a zero-line-item invoice produces exactly one
`NOT_APPLICABLE` check (`scope_key="no-line-items"`), not zero checks.

`validate_line_items_to_subtotal` sums observed line amounts and compares
against the observed subtotal within `config.monetary_tolerance`; missing
subtotal or any missing line amount -> `SKIPPED` (never a manufactured
observed subtotal). Template1's subtotal check stays `SKIPPED` (its five
line amounts are all `None` in the golden Phase 4 baseline).

`validate_invoice_total` calculates `subtotal - abs(discount) + tax +
shipping` (missing discount/tax/shipping treated as zero; missing subtotal
-> `SKIPPED`, never inferred). Observed total missing -> `REVIEW_REQUIRED`
with `TOTAL_AMOUNT_MISSING` and `CALCULATED_TOTAL_NOT_PERSISTED` -- the
calculated expectation is returned only as `expected_value` on the check,
never written into `invoice_record` (`test_invoice_total_review_required_when_total_missing_and_never_inferred`
asserts `record.get_field(TOTAL_AMOUNT) is None` immediately after the
check runs). All four fixtures' exact figures are covered end to end
(§below and the real integration run):

| File | Expected total | Observed total | Result |
|---|---:|---:|---|
| 08181_flat_document.png | 69.22 | 69.22 | PASSED |
| invoice_Aaron Bergman_36258.pdf | 50.10 | 50.10 | PASSED |
| Template1_Instance90.jpg | 882.77 | 873.58 | FAILED (difference 9.19, `INVOICE_TOTAL_MISMATCH`) |
| 08181_warped_document_perspective_shadow.jpg | 69.22 | None | REVIEW_REQUIRED |

## 12. Artifact persistence (task §16)

`persist_financial_validation_result` writes, per document, under
`<artifact_root>/<batch_id>/<document_id>/<validation_version>/`:
`financial_validation_result.json`, `validation_checks.jsonl`,
`validation_event.json`, `artifact_manifest.json` -- the notebook's exact
file set (cell 73), UTF-8, atomic (`.{name}.tmp` -> `replace`), sorted
keys, trailing newline, using the Phase 5 byte format
(`phase_5_json_safe`) kept distinct from the Phase 2/3 and Phase 4 JSON
writers (D-8 policy: three behaviourally different converters/writers
under three distinct names, never merged).

**Idempotent persistence with a collision guard (new in M6, task §16/§18;
not present in the notebook -- the same category of deliberate, tested
addition as M5's `_write_json_safe_idempotent`, decisions D-4/D-5's
precedent).** The notebook's writers always overwrote unconditionally.
Here, `_write_json_atomic_idempotent`/`_write_jsonl_atomic_idempotent`
compare the new payload against any existing file at that path (ignoring
`occurred_at`, which legitimately differs between two otherwise-identical
reprocessing runs): identical content is a silent no-op; a genuine
difference raises `FinancialValidationIntegrityError`
(`PHASE_5_ARTIFACT_COLLISION`), fail-closed. Verified by
`test_persist_is_idempotent_on_identical_rerun` (byte-identical files
across a real reprocess-and-repersist cycle) and
`test_persist_fails_closed_on_a_genuine_collision`.

Persistence order matches task §16 exactly: input-integrity verification
(`build_validation_input`, then `validate_phase_5_input_integrity` inside
`process_financial_validation`) -> all applicable checks -> summary
construction -> review routing -> result/event synchronization
(`event.status == result.status` enforced by construction) ->
persistence, called last, after the in-memory result is fully built.
`validate_persisted_phase_5_artifacts` reopens every file and asserts
persisted content matches the in-memory result exactly (also run as part
of the real Phase 1->5 integration test for all four fixtures).

## 13. Golden baseline (four fixtures)

`tests/golden/phase_5_expected_results.json`, recovered from notebook cell
74's own assertions and cross-checked against
`docs/modularisation_map.md` §10.5 and the M6 task brief §17:

| File | Status | Checks | Passed | Failed | Review | Skipped | N/A |
|---|---|---:|---:|---:|---:|---:|---:|
| Template1_Instance90.jpg | REVIEW_REQUIRED | 12 | 2 | 3 | 1 | 6 | 0 |
| 08181_flat_document.png | SUCCEEDED | 12 | 11 | 0 | 0 | 0 | 1 |
| invoice_Aaron Bergman_36258.pdf | REVIEW_REQUIRED | 8 | 6 | 0 | 1 | 0 | 1 |
| 08181_warped_document_perspective_shadow.jpg | REVIEW_REQUIRED | 12 | 7 | 0 | 4 | 0 | 1 |

Aggregate: 4 documents validated, **44 validation checks**, 1 successful
invoice, 3 review-required invoices, 0 failed production invoices --
matching the task brief's §17 aggregate exactly.

## 14. Real Phase 1 -> 5 integration

`tests/integration/test_phase_1_to_5_pipeline.py` runs the full pipeline
end to end against all four real fixtures, twice:

1. **Tesseract-forced (network-free), always runs, integrity only** --
   `test_phase_1_to_5_pipeline_integrity_on_all_four_fixtures`: SHA-256
   continuity across all five phase boundaries, stable batch/document IDs,
   the Phase 5 bridge's success path and its rejection of a live-tampered
   Phase 4 artifact, persisted/in-memory agreement, cross-document
   isolation of the Phase 5 artifact tree, and unique check IDs both
   within and across documents. **Passing.**

2. **PaddleOCR-primary, semantic parity, `requires_paddle`** --
   `test_phase_1_to_5_pipeline_final_statuses_match_the_semantic_golden_baseline`:
   asserts every field of `tests/golden/phase_5_expected_results.json`
   (status, full summary counts, all five header-check statuses, the
   invoice-total check's expected/observed values) against the real
   pipeline output, the warped invoice's never-inferred total, the
   aggregate counts, and deterministic-ID reproduction across a clean
   rerun. **This test ran successfully in this sandbox** (unlike M4/M5's
   equivalent tests, which were blocked by network egress to PaddleOCR's
   model-hosting platform): `paddlepaddle==3.3.1`/`paddleocr==3.7.0`
   installed and the engine downloaded its models over this environment's
   proxy in ~3 minutes, and **the real PaddleOCR-primary run matches the
   notebook-recovered golden baseline exactly**, with zero Tesseract
   fallback pages across all four documents. This is independent,
   live confirmation of the golden baseline in §13, not just a
   notebook-recovered one. The pre-existing M4 `requires_paddle`
   PaddleOCR-primary parity test
   (`tests/integration/test_phase_1_to_4_pipeline.py::test_phase_1_to_4_pipeline_final_statuses_match_the_semantic_golden_baseline`,
   previously blocked in the M4/M5 sandbox) was re-run in this same
   session and also **passed**, confirming this environment's network
   policy now permits PaddleOCR model downloads and that the Phase 4
   golden baseline holds against a live run too.

**Final M6 acceptance run (post-merge with `origin/main`'s M5 PaddleOCR
corrections, PR #6).** All five `requires_paddle` tests in the repository
were run together, and then the entire suite was run with no marker
exclusion at all:

```
pytest -m requires_paddle -vv
5 passed, 488 deselected in 810.44s (0:13:30)

pytest -vv -m "requires_paddle or not requires_paddle"
493 passed in 887.14s (0:14:47)
```

Zero skipped, zero deselected and zero failed in the second (full,
unfiltered) run -- every test in the repository executed and passed in a
single acceptance pass. The five `requires_paddle` tests together cover
every required real-PaddleOCR parity dimension:

| Test | Confirms |
|---|---|
| `tests/integration/test_phase_3_ocr.py::test_real_paddleocr_run_matches_the_semantic_golden_baseline` | Phase 3 PaddleOCR semantic parity against `tests/golden/phase_3_expected_results.json` |
| `tests/integration/test_phase_3_ocr.py::test_real_paddleocr_uses_paddle_on_every_fixture_page` | Every fixture page routes to `paddleocr` (4 PaddleOCR pages, one per fixture) and `fallback_pages == 0` (zero Tesseract fallback), per `tests/golden/phase_3_expected_results.json`'s `aggregate_expected.tesseract_fallback_pages == 0` |
| `tests/integration/test_phase_1_to_3_pipeline.py::test_phase_1_to_3_pipeline_final_statuses_match_the_semantic_golden_baseline` | Phase 1->3 pipeline parity, PaddleOCR primary |
| `tests/integration/test_phase_1_to_4_pipeline.py::test_phase_1_to_4_pipeline_final_statuses_match_the_semantic_golden_baseline` | Phase 1->4 normalization parity against `tests/golden/phase_4_expected_results.json` |
| `tests/integration/test_phase_1_to_5_pipeline.py::test_phase_1_to_5_pipeline_final_statuses_match_the_semantic_golden_baseline` | Phase 1->5 financial-validation parity against `tests/golden/phase_5_expected_results.json` (this milestone's own acceptance test) |

No `src/` behaviour was changed to reach this result; no golden file was
edited; no test was added, removed or reweakened between the earlier
individually-run passes (§14 above) and this combined acceptance run.

## 15. Deviations from the notebook (documented, not silently changed)

1. **Typed bridge with integrity verification** (§4) -- `build_validation_input`
   gains a normalization-version check and the new
   `verify_normalization_result_artifact_integrity` artifact-hash check;
   its four original checks now raise `FinancialValidationIntegrityError`
   instead of a bare `ValueError`. No change to which inputs are accepted
   when the artifact is genuine and untampered.
2. **Idempotent persistence with a collision guard** (§12) -- new
   behaviour, not present in the notebook; scheduled and tested the same
   way D-4/D-5/M5's Phase 4 equivalent were.
3. Inherited unchanged from M1-M5: R-14 (Phase 5 supports only
   USD/GBP/EUR, a policy carried forward, not a defect); the Phase 4/5
   `append_unique_reason` split (D-9); the Phase 2/3/4/5 `write_json_atomically`
   family staying under distinct names (D-8).

No other behaviour was changed: every check rule, reason code, message
string, rounding policy and ID formula is verbatim from its active
notebook cell (§2/§3 above), confirmed both by the real Phase 1->5
PaddleOCR-primary run (§14) and by 96 new unit/integration tests.

## 16. Complete test results

```
pytest -q -m "not requires_paddle"
488 passed, 5 deselected in ~133s

pytest -m requires_paddle -vv
5 passed, 488 deselected in 810.44s (0:13:30)

pytest -vv -m "requires_paddle or not requires_paddle"
493 passed in 887.14s (0:14:47)
```

The last of these is the final M6 acceptance run (§14): the complete
493-test suite, every marker included, 0 skipped, 0 deselected, 0 failed.

New in M6 (96 tests across five files, all passing):
- `tests/unit/test_financial_validation_tools.py` -- 49 tests (Decimal
  utilities, tolerance boundaries, deterministic IDs, explicit
  configuration, evidence operands, the check builder, required fields,
  monetary validity, date consistency, currency consistency, line
  arithmetic pass/fail/skip, subtotal pass/fail/skip, total
  pass/fail/review, missing-total non-inference, discounts/shipping/tax,
  absent optional operands, check ordering).
- `tests/unit/test_financial_validation_orchestration.py` -- 17 tests
  (the Phase 4 -> 5 bridge's six integrity checks against a real
  persisted artifact, inherited review, reason-code deduplication,
  summary counts, status routing, never-downgrading inherited review,
  result/event synchronization, and processing failure vs. financial-rule
  failure kept distinct).
- `tests/unit/test_financial_validation_artifacts.py` -- 8 tests (checks
  JSONL persistence, result/event/manifest persistence, idempotent
  rerun, different-byte collision, validation-version mismatch,
  cross-document isolation, document-isolated versioned paths).
- `tests/unit/test_financial_validation_batch_generalization.py` -- 21
  tests (more than four invoices, 0/1/many line items, all three
  supported currencies, missing optional/required fields, combined
  discount+shipping+tax, exact tolerance boundaries, malformed decimals,
  prohibited negative values by field, upstream review states,
  duplicate-looking values isolated by document ID).
- `tests/integration/test_phase_1_to_5_pipeline.py` -- 2 tests, **both
  passing** (Tesseract-forced integrity test, always run; PaddleOCR-primary
  semantic-parity test, `requires_paddle`, successfully executed in this
  sandbox -- §14).

`tests/unit/test_financial_validation.py` (24 pre-existing M2 contract
tests) is unmodified and still passes unchanged. All M1-M5 tests remain
passing. In the final acceptance run (§14), **no test is skipped or
deselected at all** -- the complete 493-test suite, `requires_paddle`
included, passes in one unfiltered run. Compliance checks re-run for M6:
no production module contains any fixture filename or expected fixture
total; no source module imports the notebook; package import with
`paddle`/`paddleocr`/`pytesseract`/`pymupdf`/`cv2`/`numpy`/`PIL`/`pandas`/
`matplotlib`/`IPython` blocked at the import hook still succeeds for every
M1-M6 module.

## 17. Unresolved risks

- R-14 (Phase 5 supports only USD/GBP/EUR; Phase 4 recognises 16 currency
  codes) remains a carried-forward policy decision, not a defect, per the
  modularisation map.
- R-11 (fixture-motivated heuristics in Phase 4) is unchanged and out of
  M6's scope; Phase 5 consumes whatever Phase 4 produces without
  reinterpreting it.
- The `requires_paddle` tests depend on the environment's ability to reach
  a PaddleOCR model-hosting platform, which succeeded throughout this
  milestone, including the final combined acceptance run (§14/§16) --
  5/5 `requires_paddle` tests passed with 0 skipped -- but is not
  guaranteed in every environment (R-07); the tests remain correctly
  marked so CI can skip them where network access is unavailable.

## 18. Readiness

**M6 is safe to merge.** Every Phase 5 function reachable from the
validated notebook call graph is extracted verbatim; the typed Phase 4 ->
5 bridge, explicit configuration, Decimal/tolerance policy, status
semantics, inherited-review propagation, required-field policy,
date/currency policy, line/subtotal/total reconciliation, missing-value
non-inference and idempotent artifact persistence are all implemented and
tested; the four-fixture golden baseline matches the notebook's own
cell-74 assertions exactly, **independently confirmed by a real
PaddleOCR-primary Phase 1->5 run in this sandbox**; and all 488
non-paddle tests plus both Phase 5 integration tests (including the
`requires_paddle` one) pass. **Final acceptance (post-merge with
`origin/main`'s M5, PR #6):** the complete, unfiltered 493-test suite
passes -- 493 passed, 0 skipped, 0 deselected, 0 failed
(`pytest -vv -m "requires_paddle or not requires_paddle"`) -- and the
five `requires_paddle` tests, run together, are 5 passed, 0 skipped, 0
failed, covering Phase 3 PaddleOCR parity, Phase 1->3, Phase 1->4
normalization parity and Phase 1->5 financial-validation parity, with
four PaddleOCR pages and zero Tesseract-fallback pages across the four
fixtures (§14).

**Notebook-to-package modularisation through Phase 5 is complete.** Every
phase (1 ingestion, 2 preprocessing, 3 OCR, 4 normalisation, 5 financial
validation) now has a modular, tested implementation with a verified
typed bridge to the next phase, matching its notebook-recorded golden
baseline.

**Development of new agent capabilities may safely resume.** The
deterministic, evidence-linked, review-routed Phase 5 result is now a
stable contract (`FinancialValidationResult`) for any future capability
(supplier matching, PO matching, approval workflows, payment scheduling)
to build on top of, without re-deriving or re-validating the financial
arithmetic itself.

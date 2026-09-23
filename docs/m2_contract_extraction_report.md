# M2 — Contract Extraction Report

**Scope:** package foundation (src layout, `pyproject.toml`) and the active,
validated contracts, enums, exceptions and configuration dataclasses/models
for Phases 1-5. No processing functions, adapters, artifact writers or
orchestrators were extracted (deferred to a later milestone).

**Source of truth:** `notebooks/accounts_payable_pipeline.ipynb` and
`docs/modularisation_map.md` (M1). The notebook was not modified, executed,
or re-run; no OCR model was downloaded.

---

## 1. Files created or populated

| File | Status |
|---|---|
| `pyproject.toml` | populated (was empty) |
| `src/ap_agent/__init__.py` | populated (`__version__` only) |
| `src/ap_agent/exceptions.py` | populated |
| `src/ap_agent/config/settings.py` | populated |
| `src/ap_agent/config/__init__.py` | left empty (C-5) |
| `src/ap_agent/models/__init__.py` | left empty (C-5) |
| `src/ap_agent/models/common.py` | populated |
| `src/ap_agent/models/ingestion.py` | populated |
| `src/ap_agent/models/preprocessing.py` | populated |
| `src/ap_agent/models/ocr.py` | populated |
| `src/ap_agent/models/normalization.py` | populated |
| `src/ap_agent/models/validation.py` | populated |
| `tests/unit/test_ingestion.py` | populated (Phase 1 + core contracts) |
| `tests/unit/test_preprocessing.py` | populated (Phase 2 contracts) |
| `tests/unit/test_ocr.py` | populated (Phase 3 contracts) |
| `tests/unit/test_normalization.py` | populated (Phase 4 contracts) |
| `tests/unit/test_financial_validation.py` | populated (Phase 5 contracts) |
| `tests/unit/test_package_foundation.py` | new: import hygiene, D-2/D-3 checks |
| `.gitignore` | populated (was empty; R-15) |
| `CLAUDE.md` | created (did not exist) |
| `docs/m2_contract_extraction_report.md` | this file |

Files intentionally **not** touched: `src/ap_agent/tools/*.py`,
`src/ap_agent/adapters/*.py`, `src/ap_agent/artifacts/*.py`,
`src/ap_agent/orchestration/*.py` (all remain empty scaffolds — processing
functions and orchestrators are out of M2 scope), the notebook, the invoice
fixtures and their manifest, `tests/integration/test_phase_1_to_5_pipeline.py`,
`tests/golden/phase_5_expected_results.json`.

## 2. Notebook definitions extracted, by phase

All cell numbers and active/superseded status are taken from
`docs/modularisation_map.md` §2 and §3.1; nothing here re-derives that
analysis.

### Core (cells 3-5, validated in cells 7-8) → `models/common.py`

`utc_now`, `ProcessingStage`, `ProcessingStatus`, `ReviewReason`,
`BatchRecord`, `DocumentRecord`, `ProcessingEvent`, `ReviewRequest`.
`ReviewReason`, `BatchRecord` and `ReviewRequest` have no caller in the
validated Phase 1-5 call graph; extracted anyway per Q-7 (part of the
validated core contract surface, needed for Phase 6+ routing).

### Phase 1 — ingestion (cell 10) → `models/ingestion.py`, `exceptions.py`, `config/settings.py`

`IngestionDisposition`, `IngestionErrorCode`, `IntakeInspection`,
`DocumentIdentity`, `IngestionResult` → `models/ingestion.py`.
`IngestionValidationError` → `exceptions.py`. `IngestionConfig` (pydantic,
frozen) → `config/settings.py`. The UUID namespaces
(`BATCH_NAMESPACE`/`CONTENT_NAMESPACE`/`DOCUMENT_NAMESPACE`), the
supported-type tables and every ingestion function (`create_batch_id` …
`ingest_document`, cells 12/14) are deferred to `tools/ingestion.py`.

### Phase 2 — preprocessing (cell 26, "PHASE 2 — CELL 1") → `models/preprocessing.py`, `config/settings.py`

`PreprocessingStatus`, `PreprocessingInput`, `PageQuality`,
`PreprocessedPage`, `PreprocessingEvent`, `PreprocessingResult` →
`models/preprocessing.py`. `PreprocessingConfig` → `config/settings.py`.
Every function in the same cell (`calculate_file_sha256`,
`write_json_atomically`, `save_png_atomically`, `load_image_with_orientation`,
`render_pdf_pages`, `load_document_pages`, `detect_skew_angle`,
`rotate_without_cropping`, `deskew_image`, `enhance_page_image`) and the
cell-29 correction `assess_page_quality` are deferred.

### Phase 3 — OCR (cell 34, "PHASE 3 — CELL 1"; `OCRPageResult` from cell 41) → `models/ocr.py`, `config/settings.py`

`OCRStatus`, `OCRPageInput`, `OCRDocumentInput`, `BoundingBox`, `OCRToken`,
`EvidenceLine`, `OCREvent`, `OCRDocumentResult` → `models/ocr.py`, all from
cell 34. `OCRPageResult` is extracted from **cell 41** ("PHASE 3 —
CORRECTION CELL 4B"), not cell 34: cell 41 superseded the cell-34
definition by adding `evidence_image_path` and `evidence_image_sha256`
(§3.1). `OCRConfig` → `config/settings.py`. Every OCR engine, adapter and
routing function (cells 36, 38, 40, 41, 43, 44, 47) is deferred, including
the four `tools/ocr_evidence.py` helpers approved by D-7.

### Phase 4 — normalization (cell 51, "PHASE 4 — CELL 1"; plus cells 54, 55, 57) → `models/normalization.py`, `config/settings.py`

From cell 51: `NormalizationStatus`, `InvoiceFieldName`,
`NormalizedValueType`, `EvidenceReferenceType`, `ExtractionMethod`,
`NormalizationInput`, `EvidenceReference`, `NormalizedValue` (type alias),
`InvoiceFieldCandidate`, `NormalizedInvoiceField`, `NormalizedLineItem`,
`NormalizedInvoiceRecord` (including its `get_field` method, which has no
external dependency), `NormalizationEvent`, `NormalizationResult`.
`NormalizationConfig` → `config/settings.py`. `normalization_utc_now` is a
processing function and is deferred.

From cell 54 ("PHASE 4 — CORRECTION CELL 2A"): `OCREvidenceIndex`, the
active definition that supersedes the cell-53 version (§3.1/§3.2 — the
cell-53 `OCREvidenceIndex`/`build_ocr_evidence_index`/
`calculate_combined_confidence` were replaced because cell 53 keyed by a
field `EvidenceReference` does not accept).

From cell 55 ("PHASE 4 — REPLACEMENT CELL 3"): `CandidateSelection`.

From cell 57 ("PHASE 4 — CELL 4"): `LineItemCandidateGroup`.

Every candidate-extraction, normalisation and persistence function from
cells 53-63 is deferred, along with the `FIELD_LABELS` and other regex/label
constants that only those functions read.

### Phase 5 — financial validation (cell 68, "PHASE 5 — CELL 1") → `models/validation.py`, `config/settings.py`

`ValidationStatus`, `ValidationCheckStatus`, `ValidationCheckType`,
`ValidationSeverity`, `ValidationInput`, `ValidationOperand`,
`ValidationCheckResult`, `FinancialValidationSummary`, `ValidationEvent`,
`FinancialValidationResult` → `models/validation.py`.
`FinancialValidationConfig` → `config/settings.py`. `validation_utc_now`
and `create_validation_id` are processing functions (the latter also reads
`financial_validation_config` as a hidden global, per §5.1) and are
deferred, along with every check-building and persistence function in
cells 69-73.

## 3. Active-definition choices (duplicate names resolved per §3.2)

| Name | Definitions considered | Extracted | Why |
|---|---|---|---|
| `OCRPageResult` | cell 34, cell 41 | **cell 41** | cell 41 adds `evidence_image_path`/`evidence_image_sha256`; cell 34's version is SUPERSEDED |
| `OCREvidenceIndex` | cell 53, cell 54 | **cell 54** | cell 54 is keyed by `reference_id`; cell 53's `token_to_evidence_reference`/`line_to_evidence_reference` passed an `evidence_id=` kwarg that `EvidenceReference` does not accept |

No other duplicate-name collision from §3.2 touches a contract, enum,
exception or config dataclass — the rest (`write_json_atomically`,
`append_unique_reason`, `calculate_file_sha256`, `process_ocr_document`,
etc.) are all processing functions, out of M2 scope by design.

## 4. Definitions deliberately deferred

Everything classified `SUPERSEDED`, `TEST_ONLY`, `DIAGNOSTIC_ONLY` or
`NOTEBOOK_ORCHESTRATION` in the modularisation map, plus every
`ACTIVE_PRODUCTION` **function** (as opposed to contract/enum/exception/
config), stays out of `src/` until the processing-extraction milestone.
That includes, notably:

- All `tools/*.py`, `adapters/*.py`, `artifacts/*.py` and
  `orchestration/*.py` functions (§2 of the modularisation map lists every
  one, with its target file).
- `tools/ocr_evidence.py` (D-7-approved new module) — reserved, per the
  task brief, for the OCR-extraction milestone.
- The two `write_json_atomically` implementations and the two
  `append_unique_reason` implementations (D-8/D-9) — not part of M2.
- `calculate_file_sha256` — a processing utility, not a contract.
- Every UUID-namespace constant and deterministic-ID formula (§1.4) —
  represented today only inside the (unused) `create_validation_id`
  reference in the notebook; no ID-generation code was extracted, so no ID
  formula could be altered even inadvertently.
- `†`-marked contracts (`ReviewReason`, `BatchRecord`, `ReviewRequest`) —
  extracted per Q-7's recommended default (see §2 above); the other two
  `†` items (`reference_matches_label`, `get_invoice_field_value`,
  `get_invoice_decimal`) are functions and stay deferred.

## 5. Import/dependency structure

```
config.settings   -> models.normalization (InvoiceFieldName, for two config defaults)
exceptions        -> models.ingestion (IngestionErrorCode)
models.ingestion  -> models.common (DocumentRecord, ProcessingEvent)
models.normalization -> models.ocr (BoundingBox, OCRDocumentResult)
models.validation -> models.normalization (NormalizedInvoiceRecord)
models.common, models.preprocessing, models.ocr -> no internal deps
```

This matches the acyclic graph in modularisation map §4.3: `config ->
models`, `exceptions -> models.ingestion`, and inside `models`:
`normalization -> ocr`, `validation -> normalization`, `ingestion ->
common`. No model imports `config` or `exceptions`. All `__init__.py`
files are empty (C-5): `import ap_agent` and every contract-module import
pull in only `pydantic` — nothing from the heavy/optional dependency list
(`pandas`, `matplotlib`, `IPython`, `paddle`, `paddleocr`, `pytesseract`,
`pymupdf`, `cv2`, `numpy`, `PIL`), verified by
`test_package_foundation.py::test_m2_modules_do_not_import_heavy_optional_dependencies`.

## 6. Deviations from the notebook contracts

1. **`OCREvidenceIndex.search()` calls a not-yet-extracted function.**
   `search()` calls `create_comparison_key`, a Phase 4 tool function
   deferred to the processing-extraction milestone. The method body is
   preserved verbatim (per the "do not blindly fix" rule); calling
   `search()` today raises `NameError`. `get()` and `page()` have no such
   dependency and work correctly today. Covered by
   `test_normalization.py::test_ocr_evidence_index_search_needs_deferred_normalization_tools`,
   which asserts the `NameError` rather than hiding it.
2. **`EvidenceReference.bounding_box` annotation vs. runtime type (R-09, not
   introduced by M2).** The field is annotated `BoundingBox` but the
   validated Phase 4 code populates it with a plain 4-tuple. Plain
   dataclasses do not validate field types at runtime, so the annotation is
   preserved as-is and both shapes are accepted — documented, not "fixed".
   Covered by
   `test_normalization.py::test_evidence_reference_bounding_box_annotation_is_not_enforced`.
3. **`from __future__ import annotations` is not module-uniform.** The
   notebook's IPython session carried this flag forward from cell 26
   onward (R-12), so cells 26, 34, 51, 54, 55, 57 and 68 all *executed*
   with postponed annotations active, even though only cells 26 and 34
   contain the literal import statement. `models/preprocessing.py`,
   `models/ocr.py`, `models/normalization.py` and `models/validation.py`
   (all downstream of cell 26, all plain-`dataclass` modules) enable it, to
   match actual notebook runtime state; this has no behavioural effect on
   plain dataclasses. `models/common.py` and `models/ingestion.py` (cells
   2-14, pydantic, ran **before** cell 26) do **not** enable it, matching
   the map's explicit caution against changing pydantic's annotation mode
   without confirming identical schema construction (§6.2 rule 2).
   `config/settings.py` aggregates one pydantic config (`IngestionConfig`,
   cell 10) with three dataclass configs from cells that ran with the flag
   active; the file does not enable it, to keep `IngestionConfig`'s pydantic
   schema construction unchanged. This has no effect on the three
   dataclass configs, whose field types are never introspected at runtime.
4. **No default `artifact_root`.** All five config classes keep
   `artifact_root` as a required field with no default, matching the
   notebook (which built `/content/...` paths as separate globals and
   passed them in explicitly). No Colab path appears anywhere in `src/`.
5. **`IngestionConfig`/`IntakeInspection`/etc. field order.** Preserved
   exactly as declared in cell 10; no reordering was applied anywhere.

No other deviation was needed: field names, field order, enum member names
and values, defaults, optionality and tuple-vs-list behaviour are extracted
verbatim from the active, validated definitions.

## 7. Test commands and results

```
$ pip install -e ".[dev]"
Successfully installed ... ap-agent-0.2.0 pydantic-2.13.5 pytest-8.4.2 ...

$ python3 -m pytest -q
78 tests collected
78 passed in 0.29s
```

Breakdown (`pytest --collect-only -q`): `test_fixture_manifest.py` (4,
pre-existing M1 tests, still green), `test_ingestion.py` (16),
`test_preprocessing.py` (6), `test_ocr.py` (7), `test_normalization.py`
(12), `test_financial_validation.py` (9), `test_package_foundation.py`
(24, parametrized import/hygiene/fixture-leakage checks). No OCR engine
ran; no fixture bytes were read except by the pre-existing manifest test.

Coverage against the task's test requirements:

- **Every extracted module imports successfully** —
  `test_package_foundation.py::test_m2_module_imports_successfully`
  (parametrized over all 9 M2 modules).
- **Enum member names/values match the notebook** — one test per enum in
  each phase's test file, comparing the full ordered `.value` list (or, for
  the two larger enums, the count plus spot checks).
- **Dataclass field names/order match the active notebook contracts** —
  exercised by construction with every field supplied positionally/by
  keyword in source order, plus one structural check
  (`test_ocr_page_result_has_evidence_image_fields`) guarding against
  accidentally extracting the superseded cell-34 shape.
- **Important defaults match** — `IngestionConfig.maximum_file_size_bytes`,
  every `*Config` default field, `ReviewRequest.blocking`,
  `NormalizedInvoiceField.review_required`, etc.
- **Contracts instantiate with representative values** — one
  "assembles the full contract" test per phase's top-level result type.
- **Immutable/tuple-based fields retain their intended behaviour** —
  `FrozenInstanceError` assertions on every `dataclass(frozen=True)` model
  touched, `ValidationError` for the two frozen pydantic models
  (`IngestionConfig`, and the extra-fields-forbidden check on `BatchRecord`),
  and explicit `== ()` / `isinstance(..., tuple)` checks on every
  `field(default_factory=tuple)`.
- **Configuration instances do not share mutable state** — one
  "instances are independent" test per config class, constructing two
  instances with different overrides and asserting neither leaks into the
  other, then asserting the second is still frozen.
- **The existing fixture-manifest test still passes** — unchanged,
  4/4 green.
- **No production module contains the four fixture filenames or the
  expected fixture totals/OCR anchors** —
  `test_package_foundation.py::test_no_production_module_contains_fixture_names_or_expected_values`,
  parametrized over the four filenames and eight recorded totals/anchors
  from §10 of the modularisation map, scanning every `.py` file under
  `src/ap_agent/`.

No OCR run and no full notebook execution were performed, per the task
brief.

## 8. Unresolved risks

- **R-02 (open, inherited from M1).** The notebook cannot be re-run "Run
  All" from a fresh runtime (cell 46 reads a name cell 48 defines). Not
  touched in M2 (the notebook is read-only) and not relevant to
  contract-only extraction.
- **R-04 (open, inherited from M1).** The Phase 3 OCR TOTAL-guard result is
  not persisted to disk in the notebook. No OCR persistence code exists
  yet in `src/`, so this cannot manifest in M2; it becomes relevant the
  moment `tools/ocr.py` is extracted (D-4/D-5).
- **New, M2-scoped: `OCREvidenceIndex.search()` is currently unusable.**
  Documented in §6.1 above and covered by an explicit test that expects
  `NameError`. This resolves itself automatically once
  `tools/normalization.py` (with `create_comparison_key`) is extracted —
  no contract change will be needed.
- **Self-test drift (R-08) has not yet been exercised.** M2 did not port
  the cell 53/55 self-asserts (they exercise *functions*, out of scope).
  When the processing-extraction milestone ports them, it must run them
  against the active cell-54/60/61 implementations, not the superseded
  cell-53/55 ones, and treat any failure as a finding.
- **Config threading (R-05) is only partially visible at this stage.**
  `NormalizationConfig.normalization_version` and
  `FinancialValidationConfig.validation_version` are represented as
  contracts now, but no code yet reads them for ID generation (no
  `create_normalization_id`/`create_validation_id` was extracted). The
  two-step gate discipline from M2.6a/b and M2.7a/b in the modularisation
  map's migration sequence still applies when those functions are
  extracted.

## 9. Readiness assessment for M3

**M3 (processing-function extraction) may begin.** All contract-level
dependencies it will need are now in place and tested:

- Every model, enum, exception and config the processing functions will
  import already exists at its final target path (§2 of the modularisation
  map), with verbatim field shapes.
- The dependency direction (`config -> models`, `exceptions ->
  models.ingestion`, `models.normalization -> models.ocr`,
  `models.validation -> models.normalization`) is established and has a
  regression test (`test_package_foundation.py`) guarding against future
  cycles and accidental heavy imports.
- `tools/ocr_evidence.py` is confirmed reserved and untouched, ready for
  the four evidence-ID/bbox helpers per D-7.
- Two known, harmless deviations are pinned down and test-covered
  (`OCREvidenceIndex.search()`, `EvidenceReference.bounding_box`), so M3
  can extract `create_comparison_key` and `extract_bounding_box` without
  first having to rediscover why the M2 contracts look the way they do.
- The duplicate-name policy for M3's actual collisions
  (`write_json_atomically` ×2, `append_unique_reason` ×2,
  `calculate_file_sha256`) is already decided (D-8/D-9/D-10) and does not
  block on anything in M2.

No blocking gaps were found.

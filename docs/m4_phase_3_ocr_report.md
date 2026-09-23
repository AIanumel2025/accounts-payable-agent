# M4B — Phase 3 OCR and Evidence Extraction Report

**Scope:** extract and validate the final, active PaddleOCR-primary /
Tesseract-fallback OCR pipeline, deterministic evidence construction, and
the critical TOTAL-value completeness guard, with a corrected persistence
order. The notebook was not modified, executed or re-run; the four
committed fixtures and their manifest were not modified.

**Source of truth:** `notebooks/accounts_payable_pipeline.ipynb`,
`docs/modularisation_map.md` (M1), `docs/m2_contract_extraction_report.md`
(M2), `docs/m3_phase_1_ingestion_report.md` (M3),
`docs/m4_phase_2_preprocessing_report.md` (M4A).

**Update (M4B resumed):** the network blocker described below has been
**resolved**. This environment's network policy was updated to permit
PaddleOCR model downloads, and the real-PaddleOCR parity run the task
brief requires has now executed successfully against all four fixtures,
matching every required semantic outcome. See §8 (resolution) and §8a
(real-run results) for the full record; §9 has the updated test totals.

---

## 1. Notebook cells used

| Cell | Content | Status |
|---:|---|---|
| 34 ("PHASE 3 — CELL 1") | OCR contracts (already in `src/` since M2); Tesseract install/version-probe scaffolding (not extracted — Colab-specific, §8 of the modularisation map) | Contracts only, no new extraction |
| 36 ("PHASE 3 — CELL 2") | Evidence-ID formulas, bounding-box utilities, Tesseract raw extraction/token/line builders; superseded `extract_ocr_page` (§3.1: orphaned, not extracted) | Extracted verbatim (evidence helpers to `tools/ocr_evidence.py`; adapter functions to `adapters/tesseract_adapter.py`) |
| 38 ("PHASE 3 — CELL 3") | `write_text_atomically`, serialisers, `build_ocr_document_directory`; superseded v1 persistence/orchestrator (`persist_ocr_page`, `process_ocr_document@38`) | Active parts extracted; superseded v1 orchestrator not extracted (§3.1) |
| 40 ("PHASE 3 — CORRECTION CELL 4A") | PaddlePaddle/PaddleOCR install scaffolding; superseded engine construction (no `enable_mkldnn`/`enable_hpi`/`cpu_threads`) | Not extracted (Colab install code + superseded engine options, §1.3) |
| 41 ("PHASE 3 — CORRECTION CELL 4B") | Active `OCRPageResult` (supersedes cell 34); PaddleOCR result adaptation, bbox conversion, evidence construction, `extract_paddle_ocr_page` | Extracted verbatim (contracts already in `src/` since M2; functions now in `adapters/paddleocr_adapter.py`) |
| 43 ("PHASE 3 — CORRECTION CELL 4C") | `phase_3_paddle_config` (a config instance, not extracted — NOTEBOOK_ORCHESTRATION); active `ocr_page_result_to_dict`; `extract_tesseract_fallback_page`; superseded `extract_routed_ocr_page@43`; `persist_routed_ocr_page`; `process_ocr_document@43` (routing + v2 persistence) | Active parts extracted; `extract_routed_ocr_page@43` not extracted (superseded by cell 44, §3.1) |
| 44 ("PHASE 3 — RUNTIME COMPATIBILITY CORRECTION") | Final, active PaddleOCR engine options (`enable_mkldnn=False`, `enable_hpi=False`, `cpu_threads=4`); final, active `extract_routed_ocr_page` | Extracted verbatim (engine options as `PaddleEngineOptions`; routing as `extract_routed_ocr_page` in `tools/ocr.py`) |
| 47 ("PHASE 3 — REPLACEMENT CELL 4E") | Critical TOTAL-value completeness guard; public `process_ocr_document` wrapper | Guard extracted verbatim; public entry point reimplemented with the corrected persistence order (§7 below) — the guard *logic* is unmodified, only *when* persistence happens changed |

Cell 46 (warped-invoice diagnostic; reads `ocr_document_results` before
cell 48 defines it, R-02) and cell 48 (ORCH + TEST + DIAG: builds OCR
inputs, runs Phase 3, anchor/determinism/invalid-hash tests, plots) were
read as the behavioural reference for the bridge function and the test
suite below; none of their diagnostic or orchestration code was copied
into `src/`.

## 2. Functions extracted, with notebook-to-module mapping

| Notebook name | Cell | Module |
|---|---:|---|
| `create_token_evidence_id` | 36 | `ap_agent.tools.ocr_evidence` |
| `create_line_evidence_id` | 36 | `ap_agent.tools.ocr_evidence` |
| `combine_bounding_boxes` | 36 | `ap_agent.tools.ocr_evidence` |
| `validate_bounding_box` | 36 | `ap_agent.tools.ocr_evidence` |
| `run_tesseract_page` | 36 | `ap_agent.adapters.tesseract_adapter` |
| `build_ocr_tokens` | 36 | `ap_agent.adapters.tesseract_adapter` |
| `build_evidence_lines` | 36 | `ap_agent.adapters.tesseract_adapter` |
| `build_raw_ocr_rows` | 36 | `ap_agent.adapters.tesseract_adapter` |
| `extract_tesseract_fallback_page` | **43** (not 36) | `ap_agent.adapters.tesseract_adapter` |
| `write_text_atomically` | 38 | `ap_agent.artifacts.filesystem` |
| `bounding_box_to_dict` | 38 | `ap_agent.artifacts.serialization` |
| `ocr_token_to_dict` | 38 | `ap_agent.artifacts.serialization` |
| `evidence_line_to_dict` | 38 | `ap_agent.artifacts.serialization` |
| `ocr_event_to_dict` | 38 | `ap_agent.artifacts.serialization` |
| `ocr_document_result_to_dict` | 38 | `ap_agent.artifacts.serialization` |
| `ocr_page_result_to_dict` | **43** (not 38) | `ap_agent.artifacts.serialization` |
| `build_ocr_document_directory` | 38 | `ap_agent.tools.ocr` |
| `make_json_compatible` | 41 | `ap_agent.artifacts.serialization` |
| `save_pil_image_atomically` | 41 | `ap_agent.artifacts.filesystem` |
| `extract_paddle_result_payload` | 41 | `ap_agent.adapters.paddleocr_adapter` |
| `extract_paddle_preprocessed_image` | 41 | `ap_agent.adapters.paddleocr_adapter` |
| `paddle_box_to_bounding_box` | 41 | `ap_agent.adapters.paddleocr_adapter` |
| `polygon_to_bounding_box` | 41 | `ap_agent.adapters.paddleocr_adapter` |
| `build_paddle_evidence` | 41 | `ap_agent.adapters.paddleocr_adapter` |
| `extract_paddle_ocr_page` | 41 | `ap_agent.adapters.paddleocr_adapter` |
| `persist_routed_ocr_page` | 43 | `ap_agent.tools.ocr` |
| `extract_routed_ocr_page` | **44** (not 43) | `ap_agent.tools.ocr` |
| `MONEY_PATTERN`, `TOTAL_PATTERN`, `TOTAL_VALUE_MISSING_REASON` | 47 | `ap_agent.tools.ocr` |
| `_box_coordinates`, `_append_reason`, `_safe_replace` | 47 | `ap_agent.tools.ocr` (private) |
| `page_has_total_without_value` | 47 | `ap_agent.tools.ocr` |
| `apply_total_completeness_guard` | 47 | `ap_agent.tools.ocr` |
| `process_ocr_document` | 43 (inner) + 47 (guard) | `ap_agent.tools.ocr` (public entry point; corrected persistence order — see §7) |

Plus three **new** functions that did not exist in the notebook:

- `build_ocr_document_input` (`ap_agent.tools.ocr`) — the explicit, typed
  Phase 2 → Phase 3 bridge (§3).
- `create_engine`, `get_paddleocr_version` (`ap_agent.adapters.paddleocr_adapter`)
  — the engine factory and lazy version probe that replace the notebook's
  `paddle_ocr_engine`/`PADDLEOCR_VERSION` hidden globals (§4).
- `get_tesseract_version` (`ap_agent.adapters.tesseract_adapter`) — replaces
  the notebook's `TESSERACT_VERSION` hidden global, read lazily.

## 3. Phase 2 → Phase 3 bridge

`build_ocr_document_input(preprocessing_result, preprocessing_version)`
reproduces notebook cell 48's bridging rule (`docs/modularisation_map.md`
§5.2 rule 2) as an explicit, typed function: it rejects a FAILED
preprocessing result (matching the notebook's assertion that preprocessing
status is not FAILED) and maps every `PreprocessedPage` to an
`OCRPageInput` with `preprocessing_review_required=page.quality.review_required`.
`preprocessing_version` is not a field of `PreprocessingResult` (the
notebook read it from the `preprocessing_config` hidden global); the
caller passes the same config instance's `preprocessing_version` explicitly.

Before OCR, `extract_paddle_ocr_page` and `extract_tesseract_fallback_page`
each independently recompute the processed page's SHA-256 and compare it
against `page_input.processed_image_sha256` (the Phase 2 evidence record),
failing closed with a message containing "SHA-256" on any mismatch —
unchanged, verbatim notebook behaviour, now exercised for both providers
via `extract_routed_ocr_page`.

## 4. Provider architecture and hidden-global corrections

- **PaddleOCR (primary), Tesseract (fallback)**: `extract_routed_ocr_page`
  (cell 44, verbatim) tries Paddle first; on any exception it falls back
  to Tesseract, marks the page `REVIEW_REQUIRED` with
  `PRIMARY_PROVIDER_FAILED` appended (deduplicated) to its review reasons,
  and re-saves a byte-stable, hash-recomputed copy of the fallback's
  coordinate image at the routed evidence-image destination. If both
  providers raise, a `RuntimeError` naming both errors propagates.
- **Explicit provider selection / lazy initialization / one reusable
  instance**: `ap_agent.adapters.paddleocr_adapter.create_engine(options)`
  builds one `PaddleOCR` instance from a `PaddleEngineOptions` dataclass
  (cell 44's final, active options: `lang=en`, `device=cpu`,
  `use_doc_orientation_classify=False`, `use_doc_unwarping=True`,
  `use_textline_orientation=True`, `enable_mkldnn=False`, `enable_hpi=False`,
  `cpu_threads=4`), called explicitly by the caller — never at import time,
  never implicitly. The `paddle.set_device(...)` side effect (notebook
  cell 40) moves inside this factory (§6.1 of the modularisation map). The
  same engine instance is passed into every `process_ocr_document` call
  for a batch, so it is built once per controlled process and reused
  across documents (this milestone's fixture-run and integration tests do
  exactly this).
- **Normalized provider output**: both adapters return an `OCRPageResult`
  in the same shape regardless of provider, plus a raw provider-output
  dict (`{"selected_provider": ..., "fallback_used": ..., "payload": ...}`)
  persisted alongside it.
- **Primary-error recording**: `raw_provider_output["primary_error"]`
  records the Paddle exception when Tesseract fallback is used.
- **Provider name and version provenance**: `OCRPageResult.ocr_engine`
  (`"paddleocr"` / `"tesseract-fallback"`, hardcoded literals — R-09,
  ignoring `OCRConfig.ocr_engine_name`, preserved not "fixed") and
  `ocr_engine_version` (the caller-supplied `engine_version` for Paddle;
  `get_tesseract_version()`, called lazily, for Tesseract).
- **Hidden-global corrections (§5.1 of the modularisation map)**:
  `extract_paddle_ocr_page` and `extract_routed_ocr_page` take `engine`
  and `engine_version` as explicit parameters instead of reading the
  notebook's `paddle_ocr_engine`/`PADDLEOCR_VERSION` globals.
  `extract_tesseract_fallback_page` calls `get_tesseract_version()`
  lazily instead of reading `TESSERACT_VERSION`. `process_ocr_document`
  accepts `engine` as a required parameter and `engine_version` as an
  optional one (defaulting to a lazy `get_paddleocr_version()` call if
  omitted).
- **Importing the package downloads nothing and initializes no engine**:
  `paddle`, `paddleocr` and `pytesseract` are imported lazily inside the
  functions/factories that need them (CLAUDE.md); verified by
  `test_processing_modules_do_not_import_heavy_optional_dependencies_at_import_time`,
  extended in this milestone to cover `ap_agent.tools.ocr`,
  `ap_agent.tools.ocr_evidence`, `ap_agent.adapters.tesseract_adapter` and
  `ap_agent.adapters.paddleocr_adapter`.
- **Local only**: no OCR call in this codebase sends invoice data to a
  network API; both providers run entirely on the local machine
  (PaddleOCR's *models* are fetched from a configured host on first engine
  construction — a one-time, explicit, caller-initiated download, not a
  per-document API call — see §8 for why that download is currently
  blocked in this environment).

## 5. Evidence-coordinate integrity

`extract_paddle_ocr_page` persists the exact image PaddleOCR used for its
detected regions — the unwarped/preprocessed derivative when Paddle's
`doc_preprocessor` produces one (`extract_paddle_preprocessed_image`),
falling back to the unmodified Phase 2 page when it does not — as
`evidence_image.png`, and every token/line bounding box
(`paddle_box_to_bounding_box`, `polygon_to_bounding_box`) is validated
against *that* image's dimensions (`validate_bounding_box`), not the
original processed-page dimensions. `extract_tesseract_fallback_page` uses
the processed page itself as its evidence image (Tesseract does no
unwarping). `extract_routed_ocr_page`'s fallback branch re-saves a
byte-stable copy of the fallback's coordinate image at the routed
destination and recomputes its hash, so the persisted `evidence_image.png`
and the `evidence_image_sha256` baked into every token/line ID always
describe the same bytes.

No diagnostic multi-panel visualization is ever selected as the evidence
image in production code (R-10's three-panel warped-invoice layout is a
notebook-only artifact of the specific validated Paddle run — this
extraction never had a diagnostic three-panel visualisation to choose
between, since `load_coordinate_aligned_evidence@48`, the diagnostic
cropper, was correctly excluded per the modularisation map, §1.2 row 103).
`test_extract_paddle_ocr_page_builds_a_succeeded_result` and
`test_extract_paddle_preprocessed_image_*` cover this path with mocked
Paddle results; §8a additionally records independent, real-evidence-image
verification (dimensions, token-bounds, coordinate-system consistency)
for all four fixtures now that a real PaddleOCR engine is available.

## 6. Critical TOTAL-value completeness guard and persistence-order correction

### 6.1 Guard (notebook cell 47, extracted verbatim)

`page_has_total_without_value` finds every token matching
`TOTAL_PATTERN` (`(?<!SUB)\bTOTAL\b`, case-insensitive — excludes
`SUBTOTAL`), picks the lowest one on the page as the grand-total label,
and returns `True` only when that label's own text has no money pattern
and no other token with a money pattern sits on the same visual line,
positioned after the label. `apply_total_completeness_guard` runs this
over every page: on a hit, it appends `CRITICAL_TOTAL_VALUE_MISSING` (once
— `_append_reason` deduplicates) to the page's `review_reasons`, forces
the page to `REVIEW_REQUIRED`, and forces the document (and its event) to
`REVIEW_REQUIRED` unless the document was already `FAILED` (a genuine
technical failure always stays `FAILED`). The guard never inserts a
monetary value anywhere — it only ever adds a reason code and flips a
status — verified by `test_apply_total_completeness_guard_never_inserts_a_money_value`.

### 6.2 Persistence-order correction (task §7; decisions D-4/D-5)

The notebook wrote `ocr_document_result.json`/`ocr_event.json`/every
`ocr_page_result.json` **inside** `process_ocr_document@43`, then applied
the guard only in memory in a thin wrapper (`process_ocr_document@47`).
For the warped fixture this meant disk said `SUCCEEDED` while the object
the notebook's own later cells (49-74, Phase 4/5) actually used said
`REVIEW_REQUIRED` — R-04, a documented, known defect.

This module's `process_ocr_document` runs the pipeline in the order the
task brief requires:

```
OCR provider result -> normalized evidence -> confidence routing
  -> TOTAL completeness guard -> final synchronized result
  -> artifact persistence
```

Concretely: `_run_ocr_pages` (the verbatim cell-43 page-processing and
status-determination logic, with all persistence removed) returns an
in-memory `OCRDocumentResult` and a `{page_number: raw_provider_output}`
map; `process_ocr_document` applies `apply_total_completeness_guard` to
that result; only the guarded, final result is passed to
`_persist_ocr_document_result`, which writes every page's artifacts
(`persist_routed_ocr_page`) and the document-level
`ocr_document_result.json`/`ocr_event.json`. The evidence PNG itself is
still written during extraction (it carries no status information and
must exist before an `OCRPageResult` can even be constructed with its
SHA-256), but every JSON record that carries a status is written **after**
the guard, so **the object `process_ocr_document` returns is always
exactly what was persisted** — a persisted `SUCCEEDED` result paired with
an in-memory `REVIEW_REQUIRED` result cannot happen. This is a deliberate,
documented, tested behaviour change from the notebook, per D-4/D-5 and the
task brief, not a silent fix: this section documents it, and every guard
and routing test also asserts persisted-vs-in-memory agreement (§9).

## 7. Files created or populated

| File | Status |
|---|---|
| `src/ap_agent/tools/ocr_evidence.py` | populated (was an empty scaffold) |
| `src/ap_agent/tools/ocr.py` | populated (was an empty scaffold) |
| `src/ap_agent/adapters/tesseract_adapter.py` | populated (was an empty scaffold) |
| `src/ap_agent/adapters/paddleocr_adapter.py` | populated (was an empty scaffold) |
| `src/ap_agent/artifacts/filesystem.py` | extended (`write_text_atomically`, `save_pil_image_atomically`) |
| `src/ap_agent/artifacts/serialization.py` | extended (Phase 3 serialisers, `make_json_compatible`) |
| `src/ap_agent/config/settings.py` | extended (`PaddleEngineOptions`) |
| `tests/unit/test_ocr_evidence.py` | new |
| `tests/unit/test_tesseract_adapter.py` | new |
| `tests/unit/test_paddleocr_adapter.py` | new |
| `tests/unit/test_ocr_orchestration.py` | new |
| `tests/integration/test_phase_3_ocr.py` | new |
| `tests/integration/test_phase_1_to_3_pipeline.py` | new |
| `tests/golden/phase_3_expected_results.json` | new |
| `tests/unit/test_package_foundation.py` | extended (4 new OCR modules added to the import-hygiene check) |
| `pyproject.toml` | `ocr-tesseract`/`ocr-paddle` extras were already added during M4A (provisioned ahead of this milestone); unchanged here |
| `docs/m4_phase_3_ocr_report.md` | this file |

No Phase 4/5 file was touched.

## 8. Dependencies, versions, and the PaddleOCR model-download blocker

| Dependency | Version installed | Notebook version (§8.1) | Notes |
|---|---|---|---|
| `pytesseract` | 0.3.x (via `pip install pytesseract`) | — | Lazy import inside `ap_agent.adapters.tesseract_adapter`. |
| System `tesseract-ocr` / `tesseract-ocr-eng` | **5.3.4** | **5.3.4** | Exact match. Installed via `apt-get install tesseract-ocr tesseract-ocr-eng`. |
| `paddlepaddle` | **3.3.1** | **3.3.1** | Exact match. Installs and imports correctly; lazy import inside `ap_agent.adapters.paddleocr_adapter`. |
| `paddleocr` | **3.7.0** | **3.7.0** | Exact match. Installs and imports correctly. |

**BLOCKER — RESOLVED.** In the original M4B pass, `paddlepaddle==3.3.1`
and `paddleocr==3.7.0` installed cleanly from PyPI and `PaddleOCR(...)`
constructed as a Python object, but the moment it tried to load its first
model it raised `Exception: No available model hosting platforms
detected. Please check your network connection.` — the environment's
egress proxy denied every one of PaddleOCR's model-hosting platforms
(`huggingface.co`, `aistudio.baidu.com`, `modelscope.cn`,
`paddle-model-ecology.bj.bcebos.com`).

This milestone was resumed after the environment's network policy was
updated to permit PaddleOCR model downloads. Engine construction
(`ap_agent.adapters.paddleocr_adapter.create_engine(PaddleEngineOptions())`)
now succeeds, and the required real-PaddleOCR fixture run has been
executed and verified. §8a below records the resolution and the results
in full; §9 has the updated test totals (**317 passed, 0 skipped, 0
failed** — no test requiring the real PaddleOCR engine remains skipped).

No model weights, downloaded fonts, caches or virtual environments are
committed (`.venv/`, PaddleX's model cache directory at
`/root/.paddlex/official_models/`, and pip's download cache are all
outside the repository and gitignored; a Python virtual environment at
`/home/user/.venv-ap`, outside the repository, was used to install the
`dev,pdf,preprocessing,ocr-tesseract,ocr-paddle` extras for this run).

## 8a. Real PaddleOCR parity run (resolution record)

### Environment and dependency versions actually used

| Component | Version | Notes |
|---|---|---|
| Python | 3.11.15 | |
| `paddlepaddle` | 3.3.1 | exact match to §8's pin |
| `paddleocr` | 3.7.0 | exact match to §8's pin |
| `numpy` | 2.3.5 | |
| `opencv-python-headless` (installed) | 4.14.0.94 | satisfies the project's `>=4,<5` extra |
| `opencv-contrib-python` (pulled in transitively by `paddleocr`/`paddlex`, shadows `cv2`) | 4.10.0.84 | `import cv2; cv2.__version__` resolves to **4.10.0** — the module actually imported at runtime is `opencv-contrib-python`, not the project's own `opencv-python-headless` pin; both satisfy `opencv-python-headless>=4,<5` on version number, and no code in `src/` depends on a feature unique to either package, so this is recorded as an environment observation, not a defect |
| `pymupdf` | 1.28.2 | exact match to the notebook-recorded version |
| `Pillow` | 12.3.0 | |
| `pytesseract` | 0.3.13 | |
| System `tesseract-ocr` binary | **5.3.4** (`tesseract 5.3.4`, leptonica-1.82.0) | installed via `apt-get install tesseract-ocr tesseract-ocr-eng`; exact match to the notebook-recorded version |

### Model-download source and identifiers

`create_engine(PaddleEngineOptions())` (defaults: `use_doc_orientation_classify=False`,
`use_doc_unwarping=True`, `use_textline_orientation=True`) downloads four
official PaddleX models on first construction, saved under
`/root/.paddlex/official_models/`:

| Model identifier | Purpose | Size | Primary source | Fallback source (actually used) |
|---|---|---|---:|---|---|
| `UVDoc` | document unwarping (`use_doc_unwarping=True`) | 31 MB | huggingface.co | — (huggingface.co succeeded) |
| `PP-LCNet_x1_0_textline_ori` | textline orientation (`use_textline_orientation=True`) | 6.6 MB | huggingface.co | — (huggingface.co succeeded) |
| `PP-OCRv6_medium_det` | text detection | 60 MB | huggingface.co | — (huggingface.co succeeded) |
| `PP-OCRv6_medium_rec` | text recognition | 74 MB | huggingface.co (attempted) | **modelscope** — huggingface.co's CAS/xet content store returned a transient `File reconstruction error` for this one model's largest asset (`inference.pdiparams`) after fetching most of it; PaddleX's own built-in multi-source fallback (`aistudio` → `huggingface` → `bos` → `modelscope`) then re-fetched the same 5 files from `modelscope.cn` (`PaddlePaddle/PP-OCRv6_medium_rec@master`) and succeeded |

Total one-time download: ~172 MB, ~739 s on first construction (engine
building/import + download); subsequent `create_engine()` calls in this
session used the on-disk cache and completed without any network
download. All four model identifiers are the ones the notebook's cell 44
active configuration selects by default — no fixture-specific or
environment-specific model override was introduced.

### Real PaddleOCR outcome per fixture (task §3)

| Fixture | Status | Provider | Tokens | Mean confidence | Required anchors captured | Required outcome met |
|---|---|---|---:|---:|---|---|
| `Template1_Instance90.jpg` | **SUCCEEDED** | `paddleocr` | 49 | 99.54 | `TAX INVOICE` ✓, `873.58` ✓ | ✓ |
| `08181_flat_document.png` | **SUCCEEDED** | `paddleocr` | 52 | 99.74 | `308044` ✓, `69.22` ✓ | ✓ |
| `invoice_Aaron Bergman_36258.pdf` | **SUCCEEDED** | `paddleocr` | 35 | 99.77 | `36258` ✓, `50.10` ✓ | ✓ |
| `08181_warped_document_perspective_shadow.jpg` | **REVIEW_REQUIRED** | `paddleocr` | 52 | 99.70 | `308044` ✓; `69.22` confirmed **absent** from OCR evidence (never inferred); `CRITICAL_TOTAL_VALUE_MISSING` present **exactly once** | ✓ |

Diagnostic-reference comparison against `docs/modularisation_map.md`
§10.3 / the golden file's carried-over diagnostic values: token counts
match exactly (49/52/35/52); mean confidences differ by ≤0.11 points
(e.g. 99.74 vs. 99.81 diagnostic reference for the flat document) — a
small, expected confidence difference across environments/runs, not a
semantic difference, and is documented here rather than used to change
any golden expectation. The warped document's evidence-image dimensions
(6360×2399) reproduce the diagnostic reference exactly.

### Provider-routing totals (task aggregate requirements)

| Metric | Required | Actual |
|---|---:|---:|
| Documents processed | 4 | 4 |
| Pages processed | 4 | 4 |
| PaddleOCR pages | 4 | 4 |
| Tesseract fallback pages | 0 | 0 |
| Successful documents | 3 | 3 |
| Review-required documents | 1 | 1 |
| Failed production documents | 0 | 0 |
| Semantic anchors captured | 7 of 8 | 7 of 8 |
| Missing anchor safely contained | yes | yes (`08181_warped_document_perspective_shadow.jpg`, `REVIEW_REQUIRED` + `CRITICAL_TOTAL_VALUE_MISSING`) |

### TOTAL completeness-guard outcome

`apply_total_completeness_guard` fired on exactly one page
(`08181_warped_document_perspective_shadow.jpg`): `CRITICAL_TOTAL_VALUE_MISSING`
appears exactly once in that page's `review_reasons`, the page and
document/event statuses were all forced to `REVIEW_REQUIRED`, and no
monetary value was inferred or written anywhere in the OCR evidence — the
missing `69.22` grand-total value is confirmed absent from
`page_text` by direct inspection of the real OCR output, not merely by
the guard's own bookkeeping. The other three fixtures triggered no guard
action.

### Evidence-coordinate validation

For every one of the four real-PaddleOCR pages: every token's bounding
box (as validated by `validate_bounding_box` at construction time, and
independently re-checked here by opening the persisted evidence PNG and
comparing pixel dimensions against every token's `bounding_box.right`/
`bounding_box.bottom`) falls within the official evidence image's actual
pixel dimensions — `Template1_Instance90.jpg`: evidence 1785×866, max
token extent 572×830; `08181_flat_document.png`: evidence 5100×2225, max
token extent 1660×1860; `invoice_Aaron Bergman_36258.pdf`: evidence
7650×3325, max token extent 2550×2276; `08181_warped_document_perspective_shadow.jpg`:
evidence 6360×2399, max token extent 2027×1822. The evidence image opened
for each check is the same file (`evidence_image.png`, by path and
SHA-256) that `OCRPageResult.evidence_image_sha256` records, confirming
tokens and evidence image share one coordinate system. No diagnostic
multi-panel visualization exists anywhere in `src/ap_agent/` (`grep -rn
"matplotlib" src/ap_agent/` returns no matches) and none was selected as
evidence for any fixture.

### Persisted/in-memory parity result

For all four fixtures: `ocr_document_result.json`'s `status`,
`ocr_event.json`'s `status` and `review_required`, and every page's
`ocr_page_result.json` `status`/`review_reasons` were read back from disk
and compared field-by-field against the in-memory `OCRDocumentResult`
`process_ocr_document` returned — **all matched, for all four fixtures,
including the warped invoice.** The warped invoice's persisted result is
`REVIEW_REQUIRED` (not the stale pre-guard `SUCCEEDED` that the
unmodified notebook would have written — R-04, corrected per §6.2), and
this is now demonstrated with the real PaddleOCR provider, not only the
forced-Tesseract path checked in the original M4B pass. Evidence
isolation was independently re-verified: no other fixture's document ID
string appears anywhere under any fixture's own Phase 3 artifact
directory.

### Phase 1 → Phase 2 → Phase 3 integration result (real PaddleOCR, not forced Tesseract)

`tests/integration/test_phase_1_to_3_pipeline.py::test_phase_1_to_3_pipeline_final_statuses_match_the_semantic_golden_baseline`
was extended in this resumption to run the same integrity checks the
Tesseract-forced test already ran
(`_assert_pipeline_integrity`, shared by both tests) — with PaddleOCR as
the primary provider for all four fixtures, end to end through ingestion
→ preprocessing → OCR:

- SHA-256 continuity across all three phase boundaries: verified for all
  four fixtures.
- Batch ID and document ID stable and identical across Phase 1, Phase 2
  and Phase 3 results: verified for all four fixtures.
- Every Phase 2 page correctly associated with its document directory;
  every Phase 3 page input consumes the exact Phase 2 artifact path and
  hash it was built from: verified.
- No cross-document evidence leakage (no other fixture's document ID
  string appears in any fixture's artifact tree): verified.
- Persisted/in-memory agreement (document, event, and every page):
  verified.
- Final status for every fixture matches
  `tests/golden/phase_3_expected_results.json`: verified (see table
  above).

This test (`pytest.mark.requires_paddle`) passed on this run.

## 9. Tests and results

**Updated (M4B resumed, real PaddleOCR now available):**

```
$ python -m pytest -m requires_paddle -vv
3 passed, 314 deselected in 555.63s (0:09:15)

$ python -m pytest -vv
317 passed, 0 skipped, 0 failed in 639.74s (0:10:39)
```

No test requiring the real PaddleOCR engine remains skipped. The three
`requires_paddle`-marked tests
(`tests/integration/test_phase_3_ocr.py::test_real_paddleocr_run_matches_the_semantic_golden_baseline`,
`tests/integration/test_phase_3_ocr.py::test_real_paddleocr_uses_paddle_on_every_fixture_page`,
`tests/integration/test_phase_1_to_3_pipeline.py::test_phase_1_to_3_pipeline_final_statuses_match_the_semantic_golden_baseline`)
built a real `PaddleOCR` engine via `create_engine()` and ran against real
fixture bytes with no mocking, no Tesseract-fallback forcing, and no
weakened/xfail'd assertions — see §8a for the full result breakdown.
`python -m compileall -q src/ap_agent` succeeds.

Test count: **317** (314 unconditional + 3 `requires_paddle`), unchanged
from the original M4B pass's total of 317 (314 passed + 3 skipped) — the
only change in this resumption is that the pre-existing
`test_phase_1_to_3_pipeline_final_statuses_match_the_semantic_golden_baseline`
was extended in place (not duplicated) to also run the shared
`_assert_pipeline_integrity` helper against the real-Paddle records, so
no new test function was added and the total stays at 317.

<details>
<summary>Original M4B pass result (superseded, kept for history)</summary>

```
$ python3 -m pytest -q
314 passed, 3 skipped in ~66s
```

The 3 skips were exactly the three real-PaddleOCR tests named in the
original §8 — every other test, including every fixture-based OCR
integration test, passed for real. `python3 -m compileall -q src/ap_agent`
succeeded.

Breakdown of new tests: 21 (`test_ocr_evidence.py`) + 12
(`test_tesseract_adapter.py`) + 28 (`test_paddleocr_adapter.py`) + 31
(`test_ocr_orchestration.py`) + 5 (`test_phase_3_ocr.py`, 3 passed + 2
skipped) + 2 (`test_phase_1_to_3_pipeline.py`, 1 passed + 1 skipped) + 4
new parametrized cases in `test_package_foundation.py` (the
`PROCESSING_MODULES` list grew from 3 to 7 modules) = **103 new tests**
(99 passing unconditionally + 4 that required a real PaddleOCR engine).

</details>

Coverage against the task's M4B test list (§9):

- **Paddle result adaptation** — `test_extract_paddle_result_payload_*`
  (unwraps a `res` key, parses a JSON string, calls a callable `.json`,
  rejects an unsupported shape, converts numpy values).
- **Tesseract result adaptation** — `test_run_tesseract_page_*`,
  `test_build_ocr_tokens_*`, `test_extract_tesseract_fallback_page_*` (all
  against the real local Tesseract binary).
- **Confidence normalization** — `test_build_paddle_evidence_converts_confidence_to_a_0_100_scale`.
- **Bounding boxes** — `test_paddle_box_to_bounding_box_*`,
  `test_polygon_to_bounding_box_*`, plus the full `test_ocr_evidence.py`
  `validate_bounding_box`/`combine_bounding_boxes` suite.
- **Deterministic token and evidence IDs** —
  `test_create_token_evidence_id_is_deterministic`,
  `test_create_token_evidence_id_changes_with_any_identity_component`
  (parametrized over every formula component),
  `test_token_and_line_evidence_ids_differ_for_identical_inputs`,
  `test_evidence_ids_are_unique_across_documents`,
  `test_build_paddle_evidence_token_id_uses_paddle_suffixed_ocr_version`
  (the `-paddle` suffix, R-09).
- **Reading order** — `test_build_paddle_evidence_orders_by_y_then_x`,
  `test_build_ocr_tokens_assigns_sequential_reading_order`.
- **Evidence-line construction** —
  `test_build_evidence_lines_groups_tokens_by_block_paragraph_line`.
- **Evidence-coordinate bounds** — every `validate_bounding_box` test,
  plus `test_extract_paddle_ocr_page_builds_a_succeeded_result` (bounds
  validated against the actual saved evidence image).
- **Page-text construction** — asserted throughout
  `test_ocr_orchestration.py` and the Tesseract adapter tests
  (`page_text` built by joining evidence-line text).
- **Provider provenance** — `ocr_engine`/`ocr_engine_version` assertions in
  `test_extract_tesseract_fallback_page_produces_a_succeeded_result` and
  `test_extract_paddle_ocr_page_builds_a_succeeded_result`.
- **Primary-provider success** — `test_extract_routed_ocr_page_uses_paddle_when_it_succeeds`.
- **Primary failure followed by fallback** —
  `test_extract_routed_ocr_page_falls_back_to_tesseract_on_paddle_failure`
  (asserts `PRIMARY_PROVIDER_FAILED`, the re-saved coordinate-consistent
  evidence image, and the recorded `primary_error`).
- **Both providers failing** —
  `test_extract_routed_ocr_page_raises_when_both_providers_fail`,
  `test_process_ocr_document_fails_closed_on_both_providers_failing`.
- **Missing or tampered Phase 2 page artifact** —
  `test_extract_paddle_ocr_page_verifies_source_hash`,
  `test_extract_tesseract_fallback_page_verifies_the_source_hash`,
  `test_process_ocr_document_fails_closed_on_a_tampered_phase_2_artifact`,
  `test_process_ocr_document_fails_closed_on_a_missing_phase_2_artifact`,
  `test_invalid_page_hash_fails_closed_on_a_real_fixture` (real fixture).
- **Review-reason uniqueness** —
  `test_apply_total_completeness_guard_adds_the_reason_once`,
  `test_process_ocr_document_review_reasons_are_unique`.
- **Event/result status synchronization** —
  `test_apply_total_completeness_guard_marks_page_and_document_review_required`,
  `test_process_ocr_document_persists_the_guarded_result_not_the_pre_guard_one`,
  `test_process_ocr_document_persisted_and_in_memory_results_always_agree`.
- **TOTAL label with value on the same token** —
  `test_page_has_total_without_value_false_when_value_is_on_the_same_token`.
- **TOTAL label with a separate value to its right** —
  `test_page_has_total_without_value_false_when_value_is_to_the_right_on_the_same_line`.
- **Subtotal labels not mistaken for grand totals** —
  `test_page_has_total_without_value_ignores_subtotal_labels`.
- **TOTAL label without a value** —
  `test_page_has_total_without_value_true_when_no_money_on_the_line`,
  `test_page_has_total_without_value_picks_the_lowest_total_as_grand_total`.
- **Missing-total non-inference** —
  `test_apply_total_completeness_guard_never_inserts_a_money_value`, and
  the real-fixture golden test's `expected_anchors_absent` assertion
  (`69.22` must never appear in the warped invoice's OCR evidence) — this
  assertion now runs for real against the real PaddleOCR engine and
  passes (§8a).
- **Final-result persistence after the guard** — §6.2 above;
  `test_process_ocr_document_persists_the_guarded_result_not_the_pre_guard_one`
  is the direct test (a page that "succeeds" at the provider level but
  trips the guard must be persisted as `REVIEW_REQUIRED`, never
  `SUCCEEDED`).
- **Cross-document isolation** —
  `test_process_ocr_document_isolates_artifacts_across_documents`,
  `test_cross_document_isolation_with_real_fixtures`, and the dedicated
  check in `test_phase_1_to_3_pipeline_integrity_on_all_four_fixtures`.
- **Idempotent rerun** — `test_process_ocr_document_rerun_is_idempotent`.

Real PaddleOCR integration run: **executed and passing** (§8a) — all
three `requires_paddle` tests run for real against the real PaddleOCR
engine on real fixture bytes, with no mocking and no forced Tesseract
fallback, and match the semantic golden baseline. The network-free OCR
run on all four fixtures via forced Tesseract fallback (§4, §6) continues
to pass as well and remains part of the suite.

## 10. Deviations from notebook behaviour

1. **Persistence-order correction (§6.2, D-4/D-5, task-required)**: the
   guard now runs before any status-bearing artifact is written. This is
   the one substantive behaviour change; it is intentional, documented,
   and tested (§6.2, §9).
2. **Hidden-global corrections (§4, §5.1 of the modularisation map)**:
   `engine`, `engine_version` and (lazily) the Tesseract version are
   explicit parameters/function calls instead of notebook globals. No
   change to what gets computed — only to how it is threaded.
3. **R-09, preserved not fixed**: `ocr_engine` literals ignore
   `OCRConfig.ocr_engine_name`; the Paddle `ocr_configuration` string
   omits the `enable_mkldnn`/`enable_hpi` flags; Paddle evidence IDs use
   `config.ocr_version + "-paddle"` (`"ocr-v2-paddle-paddle"`). All three
   are documented, not "cleaned up," per CLAUDE.md.
4. **R-10, preserved**: nothing in this milestone changes how PaddleOCR's
   own unwarped/preprocessed image is chosen as the evidence image; no
   diagnostic multi-panel visualisation is ever selected as evidence in
   production code (§5).

No other deviation was needed: function names, branch order, message
strings, reason codes, JSON key names/order and the evidence-ID formulas
are extracted verbatim from cells 36, 38, 41, 43, 44 and 47.

## 11. Unresolved risks / open items

- **The PaddleOCR network blocker (§8) is now resolved** — see §8a for
  the full real-run record. No remaining item blocks parity sign-off for
  M4B's own scope.
- **R-06 (inherited, now partially addressed)**: exact (Tier X) token
  counts reproduced exactly against the diagnostic reference (49/52/35/52);
  mean confidences differ by ≤0.11 points from the diagnostic reference
  values, which is expected run-to-run/engine-build variance and is
  documented in §8a rather than used to change any golden expectation.
  Evidence-image byte hashes remain environment-pinned (not compared
  byte-for-byte against the notebook's own run) — the golden file's
  enforced assertions are the semantic fields (status, provider, anchor
  presence/absence, guard reason), per the task brief's own instruction
  on this point, and all of them now pass against the real engine.
- **R-02, R-04 (inherited, R-04 resolved in `src/`)**: R-04 (stale
  Phase 3 OCR artifact status) is fixed in this module (§6.2) and is now
  demonstrated with the real PaddleOCR provider, not only the forced-
  Tesseract path (§8a, persisted/in-memory parity). The notebook itself
  remains unmodified and still carries the original defect, as required
  (the notebook is read-only source of truth, never "fixed").
- **Minor, non-blocking environment observation**: `opencv-contrib-python`
  (a transitive dependency of `paddleocr`/`paddlex`) shadows the
  project's own pinned `opencv-python-headless` in `import cv2` version
  resolution in this environment (§8a dependency table). Both satisfy the
  project's `>=4,<5` constraint and no code in `src/` depends on a
  feature unique to either package; recorded for completeness, not acted
  on.

## 12. Readiness assessment

**M4B is now complete, including the real-PaddleOCR parity run.** Full
suite: **317 passed, 0 skipped, 0 failed** (§9). All three
`requires_paddle` tests pass against a real, network-initialized
PaddleOCR engine on real fixture bytes, with no mocking, no forced
Tesseract fallback, and no weakened assertions. Every task-required
semantic outcome (§8a) — per-fixture status/provider/anchors, the TOTAL
completeness guard firing exactly once and containing the one missing
anchor via `REVIEW_REQUIRED` rather than inferring it, evidence-coordinate
validity, persisted/in-memory parity (including the previously-only-
Tesseract-verified warped-invoice case), and the full Phase 1 → Phase 2 →
Phase 3 integration with PaddleOCR as the primary provider — is
reproduced and verified.

**Real PaddleOCR parity is now demonstrated.** Combined M4 (M4A + M4B) has
no remaining open blocker from this milestone's own scope.
[PR #4](https://github.com/AIanumel2025/accounts-payable-agent/pull/4) is,
on these results, **ready to merge** from a correctness/testing
standpoint; per this task's explicit instructions this session does not
merge it, and Phase 4 (normalisation) modularisation is intentionally not
started here — both remain for a subsequent, separately-scoped session.

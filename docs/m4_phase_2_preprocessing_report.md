# M4A — Phase 2 Preprocessing Extraction Report

**Scope:** extract and validate the final, active Phase 2 (document
preprocessing) implementation. No Phase 3 work was started. The notebook
was not modified, executed or re-run; the four committed fixtures and
their manifest were not modified.

**Source of truth:** `notebooks/accounts_payable_pipeline.ipynb`,
`docs/modularisation_map.md` (M1), `docs/m2_contract_extraction_report.md`
(M2), `docs/m3_phase_1_ingestion_report.md` (M3).

---

## 1. Notebook cells used

| Cell | Content | Status |
|---:|---|---|
| 26 ("PHASE 2 — CELL 1") | Image loading, PDF rendering, deskew and enhancement utilities; also the superseded cell-26 `assess_page_quality`, `PreprocessingConfig`/contracts (already in `src/` since M2), and the Phase 2/3 artifact writers (`calculate_file_sha256`, `write_json_atomically`, `save_png_atomically`) | Utilities extracted verbatim; superseded `assess_page_quality` **not** extracted (§3.1) |
| 28 ("PHASE 2 — CELL 2") | Serialisers (`page_quality_to_dict`, `preprocessing_event_to_dict`, `preprocessed_page_to_dict`), `build_preprocessing_directory`, `preprocess_document` orchestrator | Extracted verbatim |
| 29 ("PHASE 2 — QUALITY-GATE CORRECTION") | The final, active `assess_page_quality` (500x700 resolution thresholds, foreground-aware `POSSIBLY_BLANK_OR_OVEREXPOSED`) | Extracted verbatim (§3.1: "Extract @29 only") |

Cell 31 ("PHASE 2 — CELL 3": real-document preprocessing, assertions,
plots) was read as the behavioural reference for the test suite and the
validated configuration values (§10.2 of the modularisation map); none of
its orchestration/test/diagnostic code was copied into `src/`.

No duplicate-name collision touches Phase 2 apart from `assess_page_quality`
itself (already resolved per §3.1/§3.2: cell 29 wins) and the three
Phase-2/3-scoped artifact-writer names (`calculate_file_sha256`,
`write_json_atomically`), which are the cell-26 binding, not the
cell-63/73 rebindings (D-8, unaffected by M4A since Phase 4/5 are not
extracted yet).

## 2. Functions extracted, with notebook-to-module mapping

| Notebook name | Cell | Module |
|---|---:|---|
| `calculate_file_sha256` | 26 | `ap_agent.artifacts.filesystem` |
| `write_json_atomically` | 26 | `ap_agent.artifacts.filesystem` |
| `save_png_atomically` | 26 | `ap_agent.artifacts.filesystem` |
| `page_quality_to_dict` | 28 | `ap_agent.artifacts.serialization` |
| `preprocessing_event_to_dict` | 28 | `ap_agent.artifacts.serialization` |
| `preprocessed_page_to_dict` | 28 | `ap_agent.artifacts.serialization` |
| `load_image_with_orientation` | 26 | `ap_agent.tools.preprocessing` |
| `render_pdf_pages` | 26 | `ap_agent.tools.preprocessing` |
| `load_document_pages` | 26 | `ap_agent.tools.preprocessing` |
| `detect_skew_angle` | 26 | `ap_agent.tools.preprocessing` |
| `rotate_without_cropping` | 26 | `ap_agent.tools.preprocessing` |
| `deskew_image` | 26 | `ap_agent.tools.preprocessing` |
| `enhance_page_image` | 26 | `ap_agent.tools.preprocessing` |
| `assess_page_quality` | **29** (not 26) | `ap_agent.tools.preprocessing` |
| `build_preprocessing_directory` | 28 | `ap_agent.tools.preprocessing` |
| `preprocess_document` | 28 | `ap_agent.tools.preprocessing` (public Phase 2 entry point) |

Plus one **new** function, `build_preprocessing_input` (`ap_agent.tools.preprocessing`),
which did not exist in the notebook — see §3 (Phase 1 → Phase 2 bridge).

Every function keeps its exact notebook name, signature, branch order,
message strings and rounding. `assess_page_quality`'s docstring notes the
cell-26→29 supersession and the two documented, preserved deviations
(R-09: hard-coded 500/700 thresholds regardless of
`config.minimum_width`/`minimum_height`; foreground-aware
`POSSIBLY_BLANK_OR_OVEREXPOSED` replacing the old unconditional
`TOO_BRIGHT`), but the code itself is unmodified from cell 29.

`import pymupdf` (not the deprecated `import fitz as pymupdf` alias the
notebook used) is used directly inside `render_pdf_pages`, per the task
brief; `pymupdf.Matrix`/`pymupdf.open` replace the notebook's
`fitz.Matrix`/`fitz.open` calls with no behavioural difference.

## 3. Phase 1 → Phase 2 bridge (deviation from the notebook, decision D-6)

The notebook's own Phase 1 → Phase 2 bridge (cell 31) built:

```python
PreprocessingInput(
    batch_id=batch_id,
    document_id=UUID(row["document_id"]),
    source_path=UPLOAD_ROOT / row["file"],     # the uploaded /content file
    source_sha256=row["sha256"],
)
```

— i.e. it re-read the originally uploaded path, not the artifact
`preserve_original_document` (Phase 1) had already written to
content-addressed storage. `docs/modularisation_map.md` records this as
**Q-3, closed by decision D-6**: "the modular Phase 2 consumes the
immutable Phase 1 preserved artifact and verifies SHA-256 continuity."

The task brief for M4A makes this an explicit requirement (§3, "Input
boundary"), so this milestone implements it as a new, small, typed bridge
function rather than silently reusing the notebook's cell-31 shape
(CLAUDE.md: "known, deliberate deviations ... must be documented, not
silently fixed, unless a milestone explicitly schedules and tests the
behaviour change" — this milestone does both):

```python
def build_preprocessing_input(ingestion_result: IngestionResult) -> PreprocessingInput:
    return PreprocessingInput(
        batch_id=ingestion_result.identity.batch_id,
        document_id=ingestion_result.identity.document_id,
        source_path=ingestion_result.stored_path,       # Phase 1's preserved artifact
        source_sha256=ingestion_result.identity.sha256,  # Phase 1's registered hash
    )
```

`preprocess_document` itself is **unchanged, verbatim cell-28 code**: it
already re-verifies `calculate_file_sha256(source_path) ==
preprocessing_input.source_sha256` and fails closed
(`PreprocessingStatus.FAILED`, `errors=(message,)`) on any mismatch before
loading a single page. Because the bridge now feeds it the preserved
artifact and the Phase 1 registered hash instead of the uploaded path, that
existing check *is* the "verify existence, verify hash, fail closed on
mismatch, never continue using an unverified document" behaviour the task
brief asks for — no new verification logic needed to be written, only the
bridge that decides which path and hash to check against.

Covered by:
`test_build_preprocessing_input_uses_the_preserved_artifact_not_the_upload_path`,
`test_phase_2_reads_the_phase_1_preserved_artifact_not_the_fixture_path`,
`test_phase_2_fails_closed_on_a_tampered_preserved_artifact`,
`test_preprocess_document_fails_closed_on_sha256_mismatch`,
`test_preprocess_document_never_uses_an_unverified_source`.

## 4. Files created or populated

| File | Status |
|---|---|
| `src/ap_agent/artifacts/filesystem.py` | populated (was an empty scaffold) |
| `src/ap_agent/artifacts/serialization.py` | populated (was an empty scaffold) |
| `src/ap_agent/tools/preprocessing.py` | populated (was an empty scaffold) |
| `tests/unit/test_preprocessing_tools.py` | new |
| `tests/unit/test_preprocessing_artifacts.py` | new |
| `tests/integration/test_phase_2_preprocessing.py` | new |
| `tests/golden/phase_2_expected_results.json` | new |
| `tests/unit/test_package_foundation.py` | extended (see §9) |
| `pyproject.toml` | extended (`pdf`, `preprocessing`, `ocr-tesseract`, `ocr-paddle` extras; `unit`/`integration`/`slow`/`ocr` markers — the `ocr-*` extras and `ocr` marker are provisioned here for M4B, not yet exercised by M4A code) |
| `docs/m4_phase_2_preprocessing_report.md` | this file |

No Phase 3+ file was touched. `src/ap_agent/tools/ocr.py`,
`src/ap_agent/tools/ocr_evidence.py` and both adapter modules remain empty
scaffolds, reserved for M4B.

## 5. Dependencies and versions

Verified installed and working in this milestone's CI environment
(`python3 --version` → 3.11.15):

| Dependency | Version | Notebook version (§8.1) | Notes |
|---|---|---|---|
| `pymupdf` | 1.28.2 | 1.28.2 | Exact match. Lazy import inside `render_pdf_pages` only. |
| `numpy` | 2.3.5 | unrecorded | R-06: not recorded by the notebook run. |
| `opencv-python-headless` | 4.10.0 | unrecorded | R-06; headless build per §8.1's packaging recommendation. |
| `Pillow` | 12.3.0 | unrecorded | R-06. |

Despite the three unpinned-in-the-notebook versions, every Tier X metric
in `docs/modularisation_map.md` §10.2 (width, height, brightness, contrast,
blur, skew, flags) was reproduced **exactly** against the four real
fixtures in this environment (§7 below) — no tolerance was needed. This is
recorded as an empirical result, not assumed: `processed_image_sha256` and
`original_render_sha256` (PNG-encoder byte output) are recorded in the
golden file as informational, pinned-environment-only values (R-06), since
encoder implementation details are not guaranteed stable across library
versions even when decoded pixels and derived metrics match exactly.

`cv2`, `numpy`, `PIL` and `pymupdf` are imported lazily inside every
function that needs them (never at module level), per CLAUDE.md; verified
by `test_processing_modules_do_not_import_heavy_optional_dependencies_at_import_time`
(a `sys.modules` diff around importing `ap_agent.artifacts.filesystem`,
`ap_agent.artifacts.serialization` and `ap_agent.tools.preprocessing`).

## 6. Artifact paths and layout

```
<artifact_root>/<batch_id>/<document_id>/<preprocessing_version>/
  preprocessing_result.json
  preprocessing_event.json
  pages/
    page_001/
      original_render.png     # unmodified rendered/loaded page (never overwritten)
      processed.png            # deskewed + enhanced, OCR-ready derivative
      quality.json
    page_002/ ...
```

Deterministic, document-isolated path formula (`build_preprocessing_directory`,
verbatim cell 28): `config.artifact_root / str(batch_id) / str(document_id)
/ config.preprocessing_version`. `preprocessing_version` defaults to
`"phase2-v1"` (`PreprocessingConfig`, unchanged since M2). JSON is written
in the **Phase 2/3 byte format** (`json.dumps(payload, indent=2,
default=str)`, ASCII-escaped) — the cell-26 `write_json_atomically`
binding, not the cell-63 Phase 4 format (D-8; Phase 4's distinct writer is
out of scope until that milestone).

## 7. Four-fixture outcomes (parity against `docs/modularisation_map.md` §10.2)

All four fixtures, run through `ingest_document` → `build_preprocessing_input`
→ `preprocess_document` with the cell-31 validated `PreprocessingConfig`
values (`pdf_dpi=300`, `enable_deskew=True`, `enable_clahe=True`,
`enable_denoising=True`, `minimum_width=500`, `minimum_height=700`,
`maximum_brightness=235.0`, every other field at its dataclass default):

| File | Status | Pages | W×H | Brightness | Contrast | Blur | Skew | Flags |
|---|---|---:|---|---:|---:|---:|---:|---|
| Template1_Instance90.jpg | SUCCEEDED | 1 | 595×841 | 240.56 | 48.03 | 5135.22 | 0.0 | none |
| 08181_flat_document.png | SUCCEEDED | 1 | 1700×2200 | 241.49 | 48.95 | 1120.34 | 0.0 | none |
| invoice_Aaron Bergman_36258.pdf | SUCCEEDED | 1 | 2550×3300 | 248.20 | 34.55 | 243.11 | 0.0 | none |
| 08181_warped_document_perspective_shadow.jpg | SUCCEEDED | 1 | 2120×2374 | 132.06 | 43.61 | 60.73 | 0.0 | none |

**All four SUCCEEDED, one page each, zero review-required, zero
failed — matching the task brief's expected fixture outcome exactly.**
Every metric above matches `docs/modularisation_map.md` §10.2 exactly (no
tolerance applied). `batch_id` (`bbf21ffd-b3e9-5086-b331-15b2138b74a3`) and
all four `document_id`s match `tests/golden/phase_1_expected_results.json`
exactly, confirming the Phase 1 → Phase 2 identity bridge is lossless.

## 8. Hash and artifact-integrity results

- **Source-hash verification**: `preprocess_document` recomputes
  `calculate_file_sha256` on the artifact `build_preprocessing_input`
  points at and compares it against `preprocessing_input.source_sha256`
  (Phase 1's registered hash) before loading any page; a mismatch fails
  closed (`FAILED`, no pages, descriptive error) without touching the
  filesystem beyond the failure JSON
  (`test_preprocess_document_never_uses_an_unverified_source`).
- **Tampered preserved-artifact test**: overwriting a fixture's *preserved*
  Phase 1 artifact bytes after ingestion, then running Phase 2 against it,
  produces `FAILED` with an error containing "SHA-256"
  (`test_phase_2_fails_closed_on_a_tampered_preserved_artifact`).
- **No source mutation**: fixture bytes under `tests/fixtures/invoices/`
  are hashed before and after the full four-fixture Phase 2 run and are
  byte-identical (`test_no_source_file_is_modified_by_preprocessing`);
  `enhance_page_image` is also proven not to mutate its input array in a
  synthetic unit test.
  `original_render.png` is likewise proven never overwritten on rerun
  (`test_preprocess_document_never_overwrites_the_original_render`).
- **Artifact isolation**: each document's directory tree is keyed by
  `document_id`; no other document's ID string appears anywhere under it
  (`test_cross_document_isolation_across_all_four_fixtures`,
  `test_preprocess_document_isolates_artifacts_across_documents`).
- **Idempotent rerun**: repeating `preprocess_document` on the same,
  still-valid input reproduces identical `processed_image_sha256` and
  `original_render_sha256` values
  (`test_preprocess_document_rerun_is_idempotent`,
  `test_repeated_preprocessing_of_the_same_document_is_idempotent`).

## 9. Tests and results

```
$ python3 -m pytest -q
214 passed in ~37s
```

Breakdown: 145 pre-existing (M1 + M2 + M3) + 31 in
`test_preprocessing_tools.py` + 23 in `test_preprocessing_artifacts.py` +
11 in `test_phase_2_preprocessing.py` + 4 new in `test_package_foundation.py`
(processing-module import hygiene, parametrized over
`ap_agent.artifacts.filesystem`, `ap_agent.artifacts.serialization`,
`ap_agent.tools.preprocessing`) = **214 total, 65 new Phase 2 tests**, plus
one existing M2 test **fixed** (not weakened — see §9.1).
`python3 -m compileall -q src/ap_agent` succeeds.

Coverage against the task's M4A test list (§6):

- **PDF rendering** — `test_render_pdf_pages_renders_every_page`,
  `test_render_pdf_pages_scales_with_dpi`,
  `test_render_pdf_pages_raises_on_empty_pdf`.
- **Raster-image loading** — `test_load_image_with_orientation_returns_bgr_array`.
- **Orientation correction** — `test_load_image_with_orientation_applies_exif_rotation`
  (a real EXIF-tagged JPEG built at test time).
- **Deskewing** — `test_detect_skew_angle_is_zero_for_a_blank_page`,
  `test_detect_skew_angle_is_zero_below_the_foreground_pixel_threshold`,
  `test_rotate_without_cropping_expands_the_canvas`,
  `test_deskew_image_leaves_a_near_zero_angle_unchanged`,
  `test_deskew_image_preserves_the_image_when_angle_exceeds_the_maximum`,
  `test_deskew_image_rotates_for_a_correctable_angle`.
- **Enhancement without source mutation** —
  `test_enhance_page_image_does_not_mutate_the_source`,
  `test_enhance_page_image_returns_a_single_channel_image`,
  `test_enhance_page_image_respects_disabled_steps`.
- **Quality measurement / corrected quality thresholds** — 13 tests in
  `test_preprocessing_tools.py` covering every flag (`LOW_RESOLUTION` at
  both the 499×700/500×699 boundary, `TOO_DARK`,
  `POSSIBLY_BLANK_OR_OVEREXPOSED` including the foreground-aware
  non-firing case, `LOW_CONTRAST`, `POSSIBLE_BLUR`,
  `EXCESSIVE_SKEW_OR_PERSPECTIVE`), plus an explicit test that
  `config.minimum_width`/`minimum_height` are ignored (R-09, preserved not
  "fixed"), plus the real-fixture Tier X/S check in the integration suite.
- **Invalid source hash** — `test_preprocess_document_fails_closed_on_sha256_mismatch`,
  `test_phase_2_fails_closed_on_a_tampered_preserved_artifact`.
- **Missing source artifact** — `test_preprocess_document_fails_closed_on_missing_source`.
- **Corrupted document** — `test_preprocess_document_fails_closed_on_corrupted_document`.
- **Unsupported input** — `test_load_document_pages_rejects_unsupported_extension`,
  `test_preprocess_document_fails_closed_on_unsupported_extension`.
- **Multipage PDF behaviour (generated document)** —
  `test_multipage_pdf_produces_one_preprocessed_page_per_pdf_page` (5 pages,
  one `PreprocessedPage` per PDF page, sequential 1-based numbering, all
  artifacts present).
- **Deterministic page and artifact identities** —
  `test_build_preprocessing_directory_is_deterministic`,
  `test_build_preprocessing_directory_isolates_documents`,
  `test_fixture_document_and_batch_ids_match_phase_1`.
- **Artifact isolation** — §8 above.
- **Artifact hash verification** — §8 above.
- **Idempotent rerun** — §8 above.
- **All four real fixtures** — §7 above, plus
  `test_fixture_artifacts_land_at_the_expected_relative_directory`.
- **A generated batch containing more than four documents** —
  `test_batch_of_more_than_four_generated_documents_are_all_processed` (9
  generated single-page PDFs, distinct document IDs, no special-casing).

All tests use `tmp_path` for runtime artifacts; none depend on execution
order or leak state between tests.

### 9.1 A pre-existing M2 test needed a false-positive fix, not a weakening

`test_no_production_module_hardcodes_a_document_count` (M2,
`test_package_foundation.py`) used a bare substring check for `"== 4"`.
`tools/preprocessing.py`'s verbatim cell-26 PDF-rendering code contains
`elif pixmap.n == 4:` — a PDF pixmap's **RGBA channel count**, unrelated to
document counting and generic to every PDF regardless of fixture count.
The check was tightened to a regex requiring the `len(...) == 4`/`== 7`
shape the test was actually designed to catch (`DOCUMENT_COUNT_PATTERN =
re.compile(r"\blen\([^)]*\)\s*==\s*[47]\b")`), which still fails on any
real document-count hardcode and no longer flags an unrelated numeric
literal. This is a false-positive correction to test infrastructure, not a
relaxation of D-2: no production code changed, and the tightened pattern
still enforces the same rule the loose one intended.

## 10. Deviations from notebook behaviour

1. **Phase 1 → Phase 2 bridge (decision D-6)** — see §3. The only
   behavioural difference from the notebook: the bridge function is new
   (the notebook had no equivalent typed function; cell 31 was inline
   orchestration), and it feeds `preprocess_document` the *preserved*
   artifact and *registered* hash instead of the *uploaded* path.
   `preprocess_document` itself is unmodified.
2. **`import pymupdf` instead of `import pymupdf as fitz`** — cosmetic
   only (task brief instruction); `pymupdf.Matrix`/`pymupdf.open` behave
   identically to the aliased calls.
3. **R-09 preserved, not fixed**: `assess_page_quality`'s `LOW_RESOLUTION`
   flag hard-codes 500×700 and ignores `config.minimum_width`/
   `minimum_height`; documented in the function's docstring and covered by
   `test_assess_page_quality_ignores_config_minimum_width_and_height`.

No other deviation was needed: field names, branch order, message
strings, JSON key names/order and rounding are extracted verbatim from
cells 26, 28 and 29.

## 11. M4A acceptance gate (task §7)

- ✅ Complete existing suite run: 214/214 passed (145 pre-existing + 69 new).
- ✅ All new Phase 2 tests run and pass (65 in the three new Phase 2 test
  files, plus 4 new import-hygiene tests).
- ✅ All four fixtures succeed (§7): 4/4 `SUCCEEDED`, 1 page each, 0 review,
  0 failed.
- ✅ Artifacts are isolated (per-`document_id` directory tree, no
  cross-document leakage) and integrity-checked (SHA-256 re-verification
  before processing, byte-identical persisted files).
- ✅ Phase 2 reads the Phase 1 preserved artifact (`stored_path`), not the
  raw fixture path (`test_phase_2_reads_the_phase_1_preserved_artifact_not_the_fixture_path`).
- ✅ No source file is modified (`test_no_source_file_is_modified_by_preprocessing`,
  plus the "original render never overwritten" test).
- Test count and results: recorded in §9 above.

**M4A passes its acceptance gate. No blocker.** Per the task brief, M4B
(Phase 3 OCR) may now begin.

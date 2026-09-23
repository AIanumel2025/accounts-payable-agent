# M3 — Phase 1 Ingestion Extraction Report

**Scope:** extract and validate the final active Phase 1 (ingestion)
implementation. No Phase 2 work was started. The notebook was not
modified, executed or re-run; no OCR model was downloaded; the four
committed fixtures and their manifest were not modified.

**Source of truth:** `notebooks/accounts_payable_pipeline.ipynb`,
`docs/modularisation_map.md` (M1), `docs/m2_contract_extraction_report.md`
(M2).

---

## 1. Active notebook cells used

| Cell | Content | Status |
|---:|---|---|
| 10 | UUID namespaces, supported-type tables, Phase 1 contracts | Constants extracted here (contracts already in `models/ingestion.py`/`config/settings.py`/`exceptions.py` since M2) |
| 12 | `create_batch_id`, `detect_document_media_type`, `validate_intake`, `calculate_sha256`, `validate_sha256`, `build_document_identity`, `is_exact_duplicate` | Extracted verbatim |
| 14 | `preserve_original_document`, `build_document_record`, `ingest_document` | Extracted verbatim |

No duplicate-name collision touches Phase 1 (`docs/modularisation_map.md`
§3.1/§3.2 list none), so there was no active-vs-superseded choice to make:
cells 12 and 14 are each defined exactly once and both are reachable from
the validated Phase 1 call cell (cell 22, per §1.2's "Call cell" table).

Cells 7–8 (core-schema construction and assertions) and cells 16–23
(synthetic ingestion tests, the real seven-file validation, and the
byte-persistence validation) were read as the behavioural reference for the
test suite below; none of their code was copied into `src/`.

## 2. Functions extracted, with notebook-to-module mapping

All ten names are extracted under their exact notebook names — no renames
were needed (no Phase 1 duplicate-name collision, §3.2).

| Notebook name | Cell | Module |
|---|---:|---|
| `create_batch_id` | 12 | `ap_agent.tools.ingestion` |
| `detect_document_media_type` | 12 | `ap_agent.tools.ingestion` |
| `validate_intake` | 12 | `ap_agent.tools.ingestion` |
| `calculate_sha256` | 12 | `ap_agent.tools.ingestion` |
| `validate_sha256` | 12 | `ap_agent.tools.ingestion` |
| `build_document_identity` | 12 | `ap_agent.tools.ingestion` |
| `is_exact_duplicate` | 12 | `ap_agent.tools.ingestion` |
| `preserve_original_document` | 14 | `ap_agent.tools.ingestion` |
| `build_document_record` | 14 | `ap_agent.tools.ingestion` |
| `ingest_document` | 14 | `ap_agent.tools.ingestion` (public Phase 1 entry point) |

Plus the four module-level constants from cell 10 (`BATCH_NAMESPACE`,
`CONTENT_NAMESPACE`, `DOCUMENT_NAMESPACE`, `SUPPORTED_DOCUMENT_TYPES`,
`CANONICAL_EXTENSION_BY_MEDIA_TYPE`), placed in `tools/ingestion.py` per
`docs/modularisation_map.md` §1.3/§6.1 (the contracts they support were
already extracted into `models/ingestion.py` during M2; the constants
themselves stay with the functions that consume them).

Every function receives its inputs explicitly (`config: IngestionConfig`,
`batch_id`, `known_sha256_values`, etc.) exactly as in the notebook — Phase
1 was never a hidden-global phase (`docs/modularisation_map.md` §5.1 lists
no Phase 1 entry), so no config-threading correction was needed.

## 3. Files created or populated

| File | Status |
|---|---|
| `src/ap_agent/tools/ingestion.py` | populated (was an empty scaffold) |
| `tests/unit/test_ingestion_tools.py` | new |
| `tests/unit/test_ingestion_artifacts.py` | new |
| `tests/integration/test_phase_1_ingestion.py` | new |
| `tests/golden/phase_1_expected_results.json` | new |
| `docs/m3_phase_1_ingestion_report.md` | this file |

**`src/ap_agent/artifacts/filesystem.py` and
`src/ap_agent/artifacts/serialization.py` were deliberately left as empty
scaffolds.** Phase 1 has no JSON serialiser and no generic atomic-write
helper of its own: `docs/modularisation_map.md` §2's active-definition
tables for both files list no entry before cell 26 (Phase 2). Phase 1's
only filesystem behaviour — copying the original into content-addressed
storage via a temp file and `os.replace` — is entirely local to
`preserve_original_document` (cell 14) and is not one of the reusable
writers (`write_json_atomically`, `save_png_atomically`, etc.) catalogued
for `artifacts/`. Inventing a generic helper for a single, already-verbatim
function would be an unrequested abstraction (CLAUDE.md: "Don't add
features, refactor, or introduce abstractions beyond what the task
requires"). No new Phase-1-scoped module was introduced either — the task
brief's "if the dependency graph requires it" condition does not apply
here: `tools/ingestion.py` alone covers every Phase 1 name with no import
cycle.

No orchestration, adapter, or Phase 2+ file was touched.

## 4. Deterministic-ID formulas preserved

Verbatim from cell 12 (`docs/modularisation_map.md` §1.4):

- `batch_id = uuid5(BATCH_NAMESPACE, f"{source.strip().lower()}:{external_reference.strip().lower()}")`, or `uuid4()` when no reference is given.
- `content_id = uuid5(CONTENT_NAMESPACE, sha256)`.
- `document_id = uuid5(DOCUMENT_NAMESPACE, f"{batch_id}:{sha256}")`.

Namespace UUIDs (unchanged): `BATCH_NAMESPACE = c251d18d-b096-47eb-908d-a86a250b2c50`, `CONTENT_NAMESPACE = 40c5b702-ffcf-4942-8e62-18b543f1d9ea`, `DOCUMENT_NAMESPACE = 63c9eed4-32bc-4340-979c-e89bd9276409`.

No timestamp, filename, or ingestion-source label enters any of these
three formulas; `test_deterministic_ids_match_the_modularisation_map_synthetic_baseline`
and `test_create_batch_id_does_not_depend_on_a_timestamp` in
`test_ingestion_tools.py` exercise this directly.

## 5. Artifact-directory layout

```
<artifact_root>/originals/<content_id>/original<canonical_extension>
```

`canonical_extension` comes from `CANONICAL_EXTENSION_BY_MEDIA_TYPE`
(`.pdf`, `.png`, `.jpg`, `.tiff`), keyed by the *detected* media type, not
the original file's extension — unchanged from the notebook. The directory
is keyed by `content_id` (not `document_id` or filename), which is what
makes renamed byte-identical copies resolve to the same artifact and
different bytes under the same filename resolve to different artifacts.

## 6. Supported input types

`.pdf` (`application/pdf`), `.png` (`image/png`), `.jpg`/`.jpeg`
(`image/jpeg`), `.tif`/`.tiff` (`image/tiff`) — `SUPPORTED_DOCUMENT_TYPES`,
verbatim from cell 10. Media type is confirmed by binary-signature sniffing
(`detect_document_media_type`), not trusted from the extension.

## 7. Duplicate and idempotency policy

- Duplicate detection is by content hash only (`is_exact_duplicate`
  against `known_sha256_values`), never by filename.
- A renamed, byte-identical file receives the *same* `content_id` and
  (within the same batch) the same `document_id` as the original, is
  disposed `DUPLICATE`/`SKIPPED`, has `original_preserved=False`, and its
  `stored_path` equals the original's.
- The same filename holding different bytes on a later call produces a
  different `content_id`/`document_id` and a different stored path; the
  first artifact is never overwritten.
- Repeated ingestion of the *same* file is idempotent:
  `preserve_original_document` reuses an existing verified artifact
  (comparing its hash against the expected identity) rather than copying
  again, and raises `HASH_MISMATCH` (fail closed) if the existing artifact
  at that content-addressed path does not match.
- The original is always copied to a temporary file first and promoted
  with `os.replace` (atomic on POSIX); a copy failure raises
  `STORAGE_FAILED` and the temporary file is removed in a `finally` block,
  so no partial artifact is ever exposed at the final path.

## 8. Failure and review-routing policy

`ingest_document` fails closed with a structured `IngestionValidationError`
(`code`, `message`, `details`) for every rejected document, matching the
notebook exactly:

| Condition | Error code |
|---|---|
| Path does not exist | `FILE_NOT_FOUND` |
| Path is not a regular file | `NOT_A_FILE` |
| Zero-byte file | `EMPTY_FILE` |
| Larger than `config.maximum_file_size_bytes` | `FILE_TOO_LARGE` |
| Extension not in `SUPPORTED_DOCUMENT_TYPES` | `UNSUPPORTED_EXTENSION` |
| Binary signature not recognized | `UNSUPPORTED_CONTENT` |
| Extension and detected content disagree | `EXTENSION_CONTENT_MISMATCH` |
| Stored/copied artifact hash does not match the registered identity | `HASH_MISMATCH` |
| Any other copy failure | `STORAGE_FAILED` |

Phase 1 has no `ReviewRequest`/review-flag concept of its own — review
routing (`ReviewReason`, `ReviewRequest`) is validated in the core-contract
cells (7–8) but is not consumed by any Phase 1 function (§1.2 of the
modularisation map marks it `†`, no caller in the validated Phase 1–5 call
graph). A freshly ingested `DocumentRecord.status` is always `PENDING`,
awaiting a later phase.

## 9. Golden-baseline values established (`tests/golden/phase_1_expected_results.json`)

Computed by calling the extracted functions against the four committed
fixtures (`tests/fixtures/invoices/`, read via `manifest.json`) and then
independently cross-checked against `docs/modularisation_map.md`:

- **Batch ID** (`create_batch_id("phase_1_colab_test", "phase-1-seven-file-validation")`): `bbf21ffd-b3e9-5086-b331-15b2138b74a3` — matches §10.1 exactly.
- **Document IDs**: Template `1c727b75-2308-5261-8805-fadca85aba7d`, flat `765ed1ee-aa8a-5de0-9342-94fbd87607f6`, Aaron `651ee283-bb83-58c3-9215-285c8e6ac801`, warped `df6c0b65-80fd-548e-a8f6-9f221eceea3a` — all four match §10.1 exactly.
- **Content IDs** (not previously recorded in the map, since M1/M2 never ran the ingestion functions): computed for the first time here and pinned in the golden file.
- **SHA-256** for every fixture matches `manifest.json` exactly (already independently verified at M1 close).
- Every fixture is disposed `ACCEPTED`, event `SUCCEEDED`, document status `PENDING`, `original_preserved=True`, and preserved at `originals/<content_id>/original<ext>`.
- The synthetic Tier-X values from §1.4 (`test_batch_id = 2e982b5f-16e5-525b-9f05-aa3bef9e78aa`, first document `65edbfaf-e250-5163-bda3-a5b4e0220465`/content `71f61c11-f36f-54b4-8357-d45c55d661a6`, different-content document `8af7823a-6b04-5469-b9f5-0849c21fd95c`) were independently reproduced by calling the extracted functions and match exactly (`test_deterministic_ids_match_the_modularisation_map_synthetic_baseline`).

No value in the golden file was invented: every field was either produced
by running the extracted code against committed bytes, or copied from a
value the modularisation map had already recorded from the notebook's own
output. The one field the task template asks for that Phase 1 does not
establish — an "expected review flag" — is recorded as an explicit,
documented omission (§8 above) rather than a guess.

## 10. Tests and results

```
$ python3 -m pytest -q
145 passed in 0.39s
```

Breakdown: 83 pre-existing (M1: 4 fixture-manifest; M2: 79 contract/package
tests) + 36 in `test_ingestion_tools.py` + 12 in `test_ingestion_artifacts.py`
+ 10 in `test_phase_1_ingestion.py`. Zero failures, zero skips, zero
warnings. `python3 -m compileall -q src/ap_agent` succeeds.

Coverage against the task's test list (§7):

- **Every supported fixture format** — `test_each_fixture_format_ingests_and_matches_the_golden_baseline` (all four fixtures: `.png`, `.jpg` ×2, `.pdf`) plus `test_detect_document_media_type_sniffs_supported_signatures` (synthetic bytes for `.pdf`/`.png`/`.jpg`/big-endian and little-endian `.tif`).
- **Correct SHA-256 values** — `test_calculate_sha256_matches_hashlib`, `test_calculate_sha256_is_correct_across_multiple_chunks`, and the fixture SHA-256 assertions in the integration test.
- **Deterministic IDs** — `test_build_document_identity_is_deterministic`, `test_build_document_identity_matches_the_uuid5_formulas`, `test_deterministic_ids_match_the_modularisation_map_synthetic_baseline`, `test_create_batch_id_is_deterministic_with_an_external_reference`, `test_fixture_document_ids_are_unique_across_documents`.
- **Successful inspection** — `test_validate_intake_accepts_a_well_formed_pdf` and the per-fixture assertions in the integration test.
- **Immutable original preservation / byte and hash equality** — `test_preserve_original_document_is_byte_for_byte`, `test_preserved_artifact_sha256_matches_the_registered_identity`, `test_original_is_preserved_byte_for_byte_for_every_fixture`.
- **Repeated ingestion (idempotency)** — `test_repeated_preservation_is_idempotent`, `test_repeated_ingestion_of_the_same_fixture_is_idempotent`.
- **Renamed exact-duplicate detection** — `test_renamed_byte_identical_copy_resolves_to_the_same_artifact` (synthetic), `test_renamed_exact_duplicate_of_a_real_fixture_is_detected` (a real fixture's bytes, renamed dynamically in `tmp_path`; no duplicate binary is committed, per CLAUDE.md).
- **Same filename, different content** — `test_same_filename_different_bytes_do_not_collide`.
- **Missing file / empty file / unsupported extension / unreadable content** — `test_validate_intake_raises_file_not_found`, `_empty_file`, `_unsupported_extension`, `_unsupported_content_for_a_fake_pdf`, `_extension_content_mismatch`, and `test_error_routing_for_missing_empty_and_unsupported_documents` (integration).
- **Artifact collision with different bytes** — `test_artifact_collision_with_different_bytes_raises_hash_mismatch` (a tampered artifact already at the content-addressed path is detected and rejected, fail-closed).
- **Review and error routing** — `test_rejected_documents_do_not_block_accepted_ones_in_the_same_batch` (a rejected document raises and does not affect the other documents' successful ingestion in the same batch).
- **Batch processing beyond four documents** — `test_batch_ingestion_with_more_than_four_generated_inputs` (4 fixtures + 3 additional synthetic documents = 7, all handled by the same loop with no special-casing).
- **Cross-document isolation** — `test_cross_document_isolation_in_the_artifact_tree`.
- **No writes outside the configured artifact root** — `test_no_writes_happen_outside_the_configured_artifact_root` (unit) and `test_no_writes_occur_outside_the_configured_artifact_root` (integration).
- **Storage-failure handling / temp-file cleanup** — `test_storage_failure_cleans_up_its_temporary_file_and_fails_closed` (a monkeypatched `shutil.copyfileobj` failure is caught, re-raised as `STORAGE_FAILED`, and leaves no stray `.ingestion-*.tmp` file behind).

All tests use `tmp_path`; none depend on execution order or on state left
by another test. No committed fixture was modified; renamed-duplicate test
files are created dynamically and discarded with the temp directory.

### 10.1 Regression checks (task §10)

- All 4 pre-existing `test_fixture_manifest.py` tests still pass.
- All 79 pre-existing M2 contract/package tests still pass.
- All 58 new Phase 1 tests pass (36 + 12 + 10).
- No production `.py` file under `src/ap_agent/` contains a fixture
  filename or an expected total/anchor (checked by grep against the same
  string list `test_package_foundation.py` already parametrizes over, plus
  a direct grep of `tools/ingestion.py`).
- No production code hardcodes a document count of 4 or 7
  (`test_no_production_module_hardcodes_a_document_count` already covers
  this repo-wide; `ingest_document`/`preserve_original_document` accept any
  iterable via the caller's loop, proven by
  `test_batch_ingestion_with_more_than_four_generated_inputs`).
- No source module imports the notebook (`grep -rl notebooks src/ap_agent`
  returns nothing).
- No source module refers to `/content` as a literal path. One pre-existing
  M2 comment in `config/settings.py` *mentions* `/content` in prose
  (explaining why `artifact_root` has no default) — that file was not
  touched in M3 and contains no `/content` path in actual code.
- No generated fixture copy or runtime artifact is committed
  (`git status` after the full test run shows only the five files listed
  in §3; nothing under a temp/artifact directory was ever created outside
  `tmp_path`).
- `python3 -m compileall -q src/ap_agent` succeeds.

## 11. Deviations from notebook behaviour

None. Every function, message string, error code, branch order, and
identifier formula was copied verbatim from cells 12 and 14. The only
non-behavioural changes are the ones CLAUDE.md and the modularisation map's
extraction rules (§6.2) explicitly permit: organizing the code into a
module with a docstring and an `__all__` list, and importing the
already-extracted M2 contracts (`IngestionConfig`, `IngestionValidationError`,
the ingestion/common models) instead of redefining them.

## 12. Unresolved risks / open items

- **Content IDs for the four fixtures were not previously recorded
  anywhere** (the modularisation map's §10.1 only records document and
  batch IDs). This M3 report and the golden file are now the first
  recorded source for them; they were derived directly from the extracted,
  verbatim `build_document_identity` formula against the committed
  fixture hashes, not guessed.
- **R-02 and R-04, inherited from M1/M2, are unaffected by M3** (they
  concern Phase 3 OCR and a notebook re-run gap that Phase 1 extraction
  does not touch).
- **`artifacts/filesystem.py` and `artifacts/serialization.py` remain
  empty.** This is a documented scope decision (§3 above), not a gap:
  Phase 1 has no serialization or generic-writer behavior to extract. M4
  (Phase 2) will be the first milestone to populate them, per the
  modularisation map's file mapping (§6.1).

## 13. Readiness assessment for M4

**M4 (Phase 2 preprocessing) may begin.** Phase 1 is now fully extracted,
verbatim, and behaviourally verified against both the four real fixtures
and the notebook's independently recorded synthetic and fixture-derived
identifiers, with zero deviations and zero open defects. The Phase 1 →
Phase 2 bridging rule (`docs/modularisation_map.md` §5.2, rule 1) needs
`IngestionResult` for every `ACCEPTED` document, which `ingest_document`
already returns unchanged. No blocking gap was found.

"""Bounded Phase 1 -> Phase 2 -> Phase 3 -> Phase 4 -> Phase 5 -> Phase 6
integration test (M7 task §8), run against all four controlled fixtures
using temporary artifact roots and the controlled prototype reference data
under `tests/fixtures/reference_data/` (no application orchestrator is
built here -- that remains out of scope until orchestration is
modularised).

Verifies:
  - SHA-256 continuity across every boundary (Phase 1 -> ... -> 6);
  - stable batch and document IDs across every phase;
  - the Phase 6 typed bridge (`build_matching_input`) accepts the real,
    consistent Phase 4/5 results and rejects a tampered one;
  - Phase 6 outcomes match tests/golden/phase_6_expected_results.json;
  - persisted and returned Phase 6 results agree;
  - deterministic matching IDs reproduce across a clean rerun;
  - no cross-document leakage in the persisted Phase 6 artifact tree;
  - no Tesseract fallback occurs in the primary parity run.

Like tests/integration/test_phase_1_to_5_pipeline.py, this runs twice: once
with the Tesseract fallback forced (network-free, always runs, checked for
integrity only), and once with PaddleOCR as the primary provider (marked
`requires_paddle`; skipped when the environment cannot reach a
model-hosting platform).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from ap_agent.adapters.reference_data_adapter import load_reference_data_bundle
from ap_agent.config.settings import (
    FinancialValidationConfig,
    IngestionConfig,
    MatchingConfig,
    NormalizationConfig,
    OCRConfig,
    PaddleEngineOptions,
    PreprocessingConfig,
)
from ap_agent.exceptions import MatchingIntegrityError
from ap_agent.models.matching import MatchingStatus, MatchMode
from ap_agent.tools.financial_validation import build_validation_input, process_financial_validation
from ap_agent.tools.ingestion import create_batch_id, ingest_document
from ap_agent.tools.matching import (
    build_matching_input,
    persist_matching_result,
    process_invoice_matching,
)
from ap_agent.tools.normalization import build_normalization_input, normalize_invoice_document
from ap_agent.tools.ocr import build_ocr_document_input, process_ocr_document
from ap_agent.tools.preprocessing import build_preprocessing_input, preprocess_document

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
REFERENCE_DATA_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "reference_data"
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "golden" / "phase_6_expected_results.json"


class _NeverAvailablePaddleEngine:
    def predict(self, path):
        raise RuntimeError("PaddleOCR intentionally disabled for this test")


def _run_full_pipeline(tmp_path, engine, engine_version):
    ingestion_config = IngestionConfig(artifact_root=tmp_path / "phase1")
    preprocessing_config = PreprocessingConfig(
        artifact_root=tmp_path / "phase2", minimum_width=500, minimum_height=700
    )
    ocr_config = OCRConfig(artifact_root=tmp_path / "phase3", ocr_engine_name="paddleocr", ocr_version="ocr-v2-paddle")
    normalization_config = NormalizationConfig(artifact_root=tmp_path / "phase4")
    financial_validation_config = FinancialValidationConfig(artifact_root=tmp_path / "phase5")
    matching_config = MatchingConfig(artifact_root=tmp_path / "phase6")

    reference_data = load_reference_data_bundle(REFERENCE_DATA_DIR)

    batch_id = create_batch_id("phase_1_colab_test", "phase-1-seven-file-validation")
    known_hashes: set[str] = set()

    records = {}

    for fixture_path in sorted(FIXTURES_DIR.iterdir()):
        if fixture_path.name == "manifest.json" or not fixture_path.is_file():
            continue

        ingestion_result = ingest_document(
            file_path=fixture_path,
            config=ingestion_config,
            batch_id=batch_id,
            ingestion_source="phase_1_colab_test",
            known_sha256_values=known_hashes,
        )
        known_hashes.add(ingestion_result.identity.sha256)

        preprocessing_input = build_preprocessing_input(ingestion_result)
        preprocessing_result = preprocess_document(preprocessing_input, preprocessing_config)

        ocr_input = build_ocr_document_input(preprocessing_result, preprocessing_config.preprocessing_version)
        ocr_result = process_ocr_document(ocr_input, ocr_config, engine=engine, engine_version=engine_version)

        normalization_input = build_normalization_input(
            ocr_result=ocr_result, preprocessing_result=preprocessing_result, ocr_config=ocr_config
        )
        normalization_result = normalize_invoice_document(normalization_input, config=normalization_config)

        validation_input = build_validation_input(normalization_result, normalization_config=normalization_config)
        financial_validation_result = process_financial_validation(validation_input, config=financial_validation_config)

        matching_input = build_matching_input(normalization_result, financial_validation_result, reference_data)
        matching_result = process_invoice_matching(matching_input, config=matching_config)
        matching_artifact_directory = persist_matching_result(matching_result, reference_data, config=matching_config)

        records[fixture_path.name] = {
            "ocr_result": ocr_result,
            "normalization_result": normalization_result,
            "financial_validation_result": financial_validation_result,
            "matching_input": matching_input,
            "matching_result": matching_result,
            "matching_artifact_directory": matching_artifact_directory,
        }

    return batch_id, records, matching_config, reference_data


def _assert_pipeline_integrity(batch_id, records, matching_config):
    assert len(records) == 4

    document_ids = [str(record["ocr_result"].document_id) for record in records.values()]

    for filename, record in records.items():
        ocr_result = record["ocr_result"]
        matching_input = record["matching_input"]
        matching_result = record["matching_result"]
        matching_artifact_directory = record["matching_artifact_directory"]

        # --- SHA-256 continuity across every phase boundary -------------
        assert matching_input.source_document_sha256 == ocr_result.source_document_sha256
        assert matching_result.source_document_sha256 == matching_input.source_document_sha256

        # --- stable batch and document IDs across every phase -----------
        assert matching_input.batch_id == ocr_result.batch_id == batch_id
        assert matching_input.document_id == ocr_result.document_id
        assert matching_result.batch_id == matching_input.batch_id
        assert matching_result.document_id == matching_input.document_id

        assert matching_result.errors == () or matching_result.status == MatchingStatus.FAILED
        assert matching_result.status != MatchingStatus.FAILED

        # --- persisted and in-memory Phase 6 results agree ---------------
        persisted_result = json.loads((matching_artifact_directory / "matching_result.json").read_text(encoding="utf-8"))
        persisted_event = json.loads((matching_artifact_directory / "matching_event.json").read_text(encoding="utf-8"))
        assert persisted_result["status"] == matching_result.status.value
        assert persisted_result["review_required"] == matching_result.review_required
        assert persisted_event["status"] == matching_result.event.status.value

        # --- deterministic Phase 6 line-match IDs are unique within this doc
        line_match_ids = [line_match.line_match_id for line_match in matching_result.line_matches]
        assert len(line_match_ids) == len(set(line_match_ids))

        # --- required artifact set -----------------------------------------
        actual_artifacts = {path.name for path in matching_artifact_directory.iterdir() if path.is_file()}
        assert actual_artifacts == {
            "matching_result.json",
            "matching_event.json",
            "line_matches.jsonl",
            "reference_snapshot.json",
            "manifest.json",
        }

        # --- no cross-document leakage in the persisted artifact tree ------
        this_document_id = str(ocr_result.document_id)
        contents = "\n".join(str(p) for p in matching_artifact_directory.rglob("*"))
        for other_document_id in document_ids:
            if other_document_id != this_document_id:
                assert other_document_id not in contents, (
                    f"{other_document_id} leaked into {filename}'s Phase 6 artifact tree"
                )

    # --- no matching_result_id or line_match_id crosses document boundaries
    all_result_ids = [record["matching_result"].matching_result_id for record in records.values()]
    assert len(all_result_ids) == len(set(all_result_ids))

    all_line_match_ids = [
        line_match.line_match_id
        for record in records.values()
        for line_match in record["matching_result"].line_matches
    ]
    assert len(all_line_match_ids) == len(set(all_line_match_ids))


@pytest.mark.integration
@pytest.mark.requires_fixtures
@pytest.mark.ocr
@pytest.mark.slow
def test_phase_1_to_6_pipeline_integrity_on_all_four_fixtures(tmp_path):
    """Network-free integrity checks, run with the Tesseract fallback
    forced so this test does not depend on PaddleOCR's model download."""

    batch_id, records, matching_config, reference_data = _run_full_pipeline(
        tmp_path, engine=_NeverAvailablePaddleEngine(), engine_version="0.0-disabled"
    )
    _assert_pipeline_integrity(batch_id, records, matching_config)

    # A tampered Phase 5 source hash must be rejected by the Phase 6 bridge.
    from dataclasses import replace

    sample = next(iter(records.values()))
    tampered_financial_result = replace(sample["financial_validation_result"], source_document_sha256="0" * 64)

    with pytest.raises(MatchingIntegrityError, match="source hashes differ"):
        build_matching_input(sample["normalization_result"], tampered_financial_result, reference_data)


@pytest.mark.integration
@pytest.mark.requires_fixtures
@pytest.mark.requires_paddle
@pytest.mark.slow
def test_phase_1_to_6_pipeline_final_statuses_match_the_semantic_golden_baseline(tmp_path):
    """The task-required semantic check: final Phase 6 statuses and matched
    references after the full Phase 1 -> ... -> 6 pipeline must match
    tests/golden/phase_6_expected_results.json, with PaddleOCR as the
    primary provider (not the forced Tesseract path) and no fallback used.
    Requires a real PaddleOCR engine; skipped when the environment cannot
    reach a model-hosting platform."""

    from ap_agent.adapters.paddleocr_adapter import create_engine, get_paddleocr_version

    try:
        engine = create_engine(PaddleEngineOptions())
    except Exception as exc:  # pragma: no cover - depends on network policy
        pytest.skip(
            "PaddleOCR engine could not be constructed (model download "
            f"blocked): {type(exc).__name__}: {exc}"
        )

    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    engine_version = get_paddleocr_version()

    batch_id, records, matching_config, reference_data = _run_full_pipeline(
        tmp_path, engine=engine, engine_version=engine_version
    )

    golden_by_filename = {doc["filename"]: doc for doc in golden["documents"]}

    for filename, expected in golden_by_filename.items():
        record = records[filename]
        ocr_result = record["ocr_result"]
        result = record["matching_result"]
        summary = result.summary

        # No Tesseract fallback in the primary parity run.
        assert all(page.ocr_engine == "paddleocr" for page in ocr_result.pages)

        assert result.status.value == expected["status"]
        assert result.supplier_resolution.status.value == expected["supplier"]
        assert result.supplier_resolution.matched_supplier_id == expected["supplier_id"]
        assert result.purchase_order_resolution.status.value == expected["purchase_order"]
        assert result.purchase_order_resolution.purchase_order_number == expected["po_number"]
        assert result.goods_receipt_resolution.status.value == expected["goods_receipt"]
        assert summary.match_mode.value == expected["match_mode"]
        assert len(result.line_matches) == expected["line_matches"]
        assert summary.checks_passed == expected["checks_passed"]
        assert summary.checks_failed == expected["checks_failed"]
        assert summary.checks_requiring_review == expected["review_checks"]
        assert summary.checks_skipped == expected["checks_skipped"]

        for reason in expected["required_reasons"]:
            assert reason in result.review_reasons

        if expected["expected_total"] is not None:
            assert summary.expected_total == Decimal(expected["expected_total"])
            assert summary.observed_total == Decimal(expected["observed_total"])
            assert summary.total_variance == Decimal(expected["total_variance"])

        assert result.errors == ()

    aggregate = golden["aggregate_expected"]
    all_results = [record["matching_result"] for record in records.values()]
    assert len(all_results) == aggregate["invoices_processed"]
    assert sum(r.status == MatchingStatus.SUCCEEDED for r in all_results) == aggregate["successful_invoices"]
    assert (
        sum(r.status == MatchingStatus.REVIEW_REQUIRED for r in all_results) == aggregate["review_required_invoices"]
    )
    assert sum(r.status == MatchingStatus.FAILED for r in all_results) == aggregate["failed_production_invoices"]
    assert sum(r.summary.match_mode == MatchMode.THREE_WAY for r in all_results) == aggregate["three_way_matches"]
    assert sum(len(r.line_matches) for r in all_results) == aggregate["line_matches_total"]

    # Deterministic IDs reproduce across a clean rerun of the same inputs.
    for filename, record in records.items():
        rerun_result = process_invoice_matching(record["matching_input"], config=matching_config)
        assert rerun_result.matching_result_id == record["matching_result"].matching_result_id

    _assert_pipeline_integrity(batch_id, records, matching_config)

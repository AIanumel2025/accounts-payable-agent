"""Bounded Phase 1 -> Phase 2 -> Phase 3 -> Phase 4 -> Phase 5 integration
test (M6 task §19), run against all four controlled fixtures using
temporary artifact roots (no application orchestrator is built here --
that remains out of scope until orchestration is modularised).

Verifies:
  - SHA-256 continuity across every boundary (Phase 1 -> 2 -> 3 -> 4 -> 5);
  - stable batch and document IDs across every phase;
  - the Phase 5 typed bridge (`build_validation_input`) accepts the real,
    persisted Phase 4 result for the correct document and rejects a
    tampered one;
  - Phase 5 checks match tests/golden/phase_5_expected_results.json;
  - persisted and returned Phase 5 results agree;
  - deterministic validation IDs reproduce across a clean rerun;
  - the warped invoice's expected total (69.22) is calculated as validation
    evidence only and never written back into the normalized invoice;
  - no cross-document leakage in the persisted Phase 5 artifact tree;
  - no Tesseract fallback occurs in the primary parity run.

Like tests/integration/test_phase_1_to_4_pipeline.py, this runs twice: once
with the Tesseract fallback forced (network-free, always runs, checked for
integrity only), and once with PaddleOCR as the primary provider (marked
`requires_paddle`; skipped when the environment cannot reach a
model-hosting platform -- see docs/m4_phase_3_ocr_report.md and
docs/m6_phase_5_financial_validation_report.md, which documents this
sandbox's network-egress blocker for PaddleOCR model downloads).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from ap_agent.config.settings import (
    FinancialValidationConfig,
    IngestionConfig,
    NormalizationConfig,
    OCRConfig,
    PaddleEngineOptions,
    PreprocessingConfig,
)
from ap_agent.models.normalization import InvoiceFieldName
from ap_agent.models.validation import ValidationCheckType, ValidationStatus
from ap_agent.tools.financial_validation import (
    build_validation_input,
    persist_financial_validation_result,
    phase_5_artifact_directory,
    process_financial_validation,
    validate_persisted_phase_5_artifacts,
)
from ap_agent.tools.ingestion import create_batch_id, ingest_document
from ap_agent.tools.normalization import build_normalization_input, normalize_invoice_document
from ap_agent.tools.ocr import build_ocr_document_input, process_ocr_document
from ap_agent.tools.preprocessing import build_preprocessing_input, preprocess_document

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "golden" / "phase_5_expected_results.json"


class _NeverAvailablePaddleEngine:
    def predict(self, path):
        raise RuntimeError("PaddleOCR intentionally disabled for this test")


def _run_full_pipeline(tmp_path, engine, engine_version):
    ingestion_config = IngestionConfig(artifact_root=tmp_path / "phase1")
    preprocessing_config = PreprocessingConfig(
        artifact_root=tmp_path / "phase2", minimum_width=500, minimum_height=700
    )
    ocr_config = OCRConfig(
        artifact_root=tmp_path / "phase3", ocr_engine_name="paddleocr", ocr_version="ocr-v2-paddle"
    )
    normalization_config = NormalizationConfig(artifact_root=tmp_path / "phase4")
    financial_validation_config = FinancialValidationConfig(artifact_root=tmp_path / "phase5")

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

        validation_input = build_validation_input(
            normalization_result, normalization_config=normalization_config
        )
        financial_validation_result = process_financial_validation(
            validation_input, config=financial_validation_config
        )
        artifact_paths = persist_financial_validation_result(
            financial_validation_result, config=financial_validation_config
        )
        validate_persisted_phase_5_artifacts(financial_validation_result, artifact_paths)

        records[fixture_path.name] = {
            "ingestion_result": ingestion_result,
            "preprocessing_result": preprocessing_result,
            "ocr_result": ocr_result,
            "normalization_result": normalization_result,
            "validation_input": validation_input,
            "financial_validation_result": financial_validation_result,
            "artifact_paths": artifact_paths,
        }

    return batch_id, records, financial_validation_config


def _assert_pipeline_integrity(batch_id, records, financial_validation_config):
    """SHA-256 continuity, ID stability, no cross-document leakage and
    persisted/in-memory agreement -- checked regardless of which OCR
    provider produced `records`."""

    assert len(records) == 4

    document_ids = [str(record["ocr_result"].document_id) for record in records.values()]

    for filename, record in records.items():
        ocr_result = record["ocr_result"]
        normalization_result = record["normalization_result"]
        validation_input = record["validation_input"]
        financial_validation_result = record["financial_validation_result"]
        artifact_paths = record["artifact_paths"]

        # --- SHA-256 continuity across every phase boundary -------------
        assert normalization_result.source_document_sha256 == ocr_result.source_document_sha256
        assert validation_input.source_document_sha256 == normalization_result.source_document_sha256
        assert financial_validation_result.source_document_sha256 == validation_input.source_document_sha256

        # --- stable batch and document IDs across every phase -----------
        assert normalization_result.batch_id == ocr_result.batch_id == batch_id
        assert normalization_result.document_id == ocr_result.document_id
        assert validation_input.batch_id == normalization_result.batch_id
        assert validation_input.document_id == normalization_result.document_id
        assert financial_validation_result.batch_id == validation_input.batch_id
        assert financial_validation_result.document_id == validation_input.document_id

        assert financial_validation_result.errors == () or financial_validation_result.status == "FAILED"
        assert financial_validation_result.status != ValidationStatus.FAILED

        # --- persisted and in-memory Phase 5 results agree ---------------
        persisted_result = json.loads(artifact_paths["result"].read_text(encoding="utf-8"))
        persisted_event = json.loads(artifact_paths["event"].read_text(encoding="utf-8"))
        assert persisted_result["status"] == financial_validation_result.status.value
        assert persisted_result["document_id"] == str(financial_validation_result.document_id)
        assert persisted_event["status"] == financial_validation_result.status.value

        # --- deterministic Phase 5 check IDs are unique within this doc --
        check_ids = [check.check_id for check in financial_validation_result.checks]
        assert len(check_ids) == len(set(check_ids))

        # --- no cross-document leakage in the persisted artifact tree ----
        this_document_id = str(ocr_result.document_id)
        artifact_directory = phase_5_artifact_directory(
            financial_validation_result, config=financial_validation_config
        )
        contents = "\n".join(str(p) for p in artifact_directory.rglob("*"))
        for other_document_id in document_ids:
            if other_document_id != this_document_id:
                assert other_document_id not in contents, (
                    f"{other_document_id} leaked into {filename}'s Phase 5 artifact tree"
                )

    # --- no check ID crosses document boundaries -------------------------
    all_check_ids = [
        check.check_id
        for record in records.values()
        for check in record["financial_validation_result"].checks
    ]
    assert len(all_check_ids) == len(set(all_check_ids))


@pytest.mark.integration
@pytest.mark.requires_fixtures
@pytest.mark.ocr
@pytest.mark.slow
def test_phase_1_to_5_pipeline_integrity_on_all_four_fixtures(tmp_path):
    """Network-free integrity checks, run with the Tesseract fallback
    forced so this test does not depend on PaddleOCR's model download."""

    batch_id, records, financial_validation_config = _run_full_pipeline(
        tmp_path, engine=_NeverAvailablePaddleEngine(), engine_version="0.0-disabled"
    )
    _assert_pipeline_integrity(batch_id, records, financial_validation_config)

    # A tampered Phase 4 artifact must be rejected by the Phase 5 bridge.
    from ap_agent.exceptions import FinancialValidationIntegrityError
    from ap_agent.config.settings import NormalizationConfig as _NormalizationConfig

    sample = next(iter(records.values()))
    normalization_config = _NormalizationConfig(artifact_root=tmp_path / "phase4")
    normalization_result = sample["normalization_result"]

    document_directory = (
        normalization_config.artifact_root
        / str(normalization_result.batch_id)
        / str(normalization_result.document_id)
        / normalization_result.normalization_version
    )
    artifact_path = document_directory / "normalization_result.json"
    original_bytes = artifact_path.read_text(encoding="utf-8")
    artifact_path.write_text(original_bytes.replace("normalization-v1", "normalization-v9"), encoding="utf-8")

    with pytest.raises(FinancialValidationIntegrityError):
        build_validation_input(normalization_result, normalization_config=normalization_config)


@pytest.mark.integration
@pytest.mark.requires_fixtures
@pytest.mark.requires_paddle
@pytest.mark.slow
def test_phase_1_to_5_pipeline_final_statuses_match_the_semantic_golden_baseline(tmp_path):
    """The task-required semantic check: final Phase 5 statuses and totals
    after the full Phase 1 -> 2 -> 3 -> 4 -> 5 pipeline must match
    tests/golden/phase_5_expected_results.json, with PaddleOCR as the
    primary provider (not the forced Tesseract path) and no fallback used.
    Requires a real PaddleOCR engine; skipped when the environment cannot
    reach a model-hosting platform (documented blocker,
    docs/m6_phase_5_financial_validation_report.md)."""

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

    batch_id, records, financial_validation_config = _run_full_pipeline(
        tmp_path, engine=engine, engine_version=engine_version
    )

    golden_by_filename = {doc["filename"]: doc for doc in golden["documents"]}

    for filename, expected in golden_by_filename.items():
        record = records[filename]
        ocr_result = record["ocr_result"]
        result = record["financial_validation_result"]

        # No Tesseract fallback in the primary parity run.
        assert all(page.ocr_engine == "paddleocr" for page in ocr_result.pages)

        assert result.status.value == expected["expected_status"]
        summary = expected["expected_summary"]
        assert result.summary.total_checks == summary["total_checks"]
        assert result.summary.passed_checks == summary["passed_checks"]
        assert result.summary.failed_checks == summary["failed_checks"]
        assert result.summary.review_required_checks == summary["review_required_checks"]
        assert result.summary.not_applicable_checks == summary["not_applicable_checks"]
        assert result.summary.skipped_checks == summary["skipped_checks"]
        assert result.summary.review_required == summary["review_required"]

        for check_type_name, expected_status in expected["expected_header_checks"].items():
            check = next(c for c in result.checks if c.check_type == ValidationCheckType[check_type_name])
            assert check.status.value == expected_status

        total_check = next(
            c for c in result.checks if c.check_type == ValidationCheckType.INVOICE_TOTAL_RECONCILIATION
        )
        expected_total_info = expected["expected_total_reconciliation"]
        assert total_check.status.value == expected_total_info["status"]
        assert total_check.expected_value == expected_total_info["expected_total"]
        assert total_check.observed_value == expected_total_info["observed_total"]

        assert result.review_required is (result.status.value != "SUCCEEDED")
        assert result.errors == ()

    # The warped invoice's calculated total must never be written back.
    warped_key = "08181_warped_document_perspective_shadow.jpg"
    warped_normalization_result = records[warped_key]["normalization_result"]
    warped_total_field = warped_normalization_result.invoice_record.get_field(InvoiceFieldName.TOTAL_AMOUNT)
    assert warped_total_field is None or warped_total_field.normalized_value is None

    warped_total_check = next(
        c
        for c in records[warped_key]["financial_validation_result"].checks
        if c.check_type == ValidationCheckType.INVOICE_TOTAL_RECONCILIATION
    )
    assert warped_total_check.expected_value == "69.22"
    assert warped_total_check.observed_value is None

    aggregate = golden["aggregate_expected"]
    all_results = [record["financial_validation_result"] for record in records.values()]
    assert len(all_results) == aggregate["documents_validated"]
    assert sum(len(r.checks) for r in all_results) == aggregate["validation_checks_total"]
    assert sum(1 for r in all_results if r.status == ValidationStatus.SUCCEEDED) == aggregate["successful_invoices"]
    assert (
        sum(1 for r in all_results if r.status == ValidationStatus.REVIEW_REQUIRED)
        == aggregate["review_required_invoices"]
    )
    assert sum(1 for r in all_results if r.status == ValidationStatus.FAILED) == aggregate["failed_production_invoices"]

    # Deterministic IDs reproduce across a clean rerun of the same inputs.
    for filename, record in records.items():
        rerun_result = process_financial_validation(record["validation_input"], config=financial_validation_config)
        first_ids = tuple(check.check_id for check in record["financial_validation_result"].checks)
        second_ids = tuple(check.check_id for check in rerun_result.checks)
        assert first_ids == second_ids

    _assert_pipeline_integrity(batch_id, records, financial_validation_config)

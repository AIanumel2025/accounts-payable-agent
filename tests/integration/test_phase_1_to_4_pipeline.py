"""Bounded Phase 1 -> Phase 2 -> Phase 3 -> Phase 4 integration test (M5
task §15), run against all four controlled fixtures using temporary
artifact roots (no application orchestrator is built here — that remains
out of scope until orchestration is modularised).

Verifies:
  - SHA-256 continuity across every boundary (Phase 1 -> 2 -> 3 -> 4);
  - stable batch and document IDs across every phase;
  - the Phase 4 typed bridge (`build_normalization_input`) accepts the real,
    persisted, post-TOTAL-guard Phase 3 result for the correct document;
  - every Phase 4 evidence reference resolves to a token/line from the
    correct document's own OCR result (no cross-document evidence);
  - no candidate or line item crosses document boundaries;
  - normalized values and final statuses match
    tests/golden/phase_4_expected_results.json;
  - persisted and returned Phase 4 results agree;
  - deterministic IDs reproduce across a clean rerun;
  - the warped invoice's normalized total stays absent (never inferred);
  - no Tesseract fallback occurs in the primary parity run.

Like tests/integration/test_phase_1_to_3_pipeline.py, this runs twice: once
with the Tesseract fallback forced (network-free, always runs, checked for
integrity only — the golden baseline describes the PaddleOCR-primary run),
and once with PaddleOCR as the primary provider (marked `requires_paddle`;
skipped when the environment cannot reach a model-hosting platform — see
docs/m4_phase_3_ocr_report.md and tests/golden/phase_3_expected_results.json,
which documents this sandbox's network-egress blocker).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from ap_agent.artifacts.filesystem import calculate_file_sha256
from ap_agent.config.settings import (
    IngestionConfig,
    NormalizationConfig,
    OCRConfig,
    PaddleEngineOptions,
    PreprocessingConfig,
)
from ap_agent.models.normalization import InvoiceFieldName, NormalizationStatus
from ap_agent.tools.ingestion import create_batch_id, ingest_document
from ap_agent.tools.normalization import (
    build_normalization_input,
    normalize_invoice_document,
    phase_4_document_directory,
)
from ap_agent.tools.ocr import build_ocr_document_input, process_ocr_document
from ap_agent.tools.preprocessing import build_preprocessing_input, preprocess_document

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "golden" / "phase_4_expected_results.json"


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

        ocr_input = build_ocr_document_input(
            preprocessing_result, preprocessing_config.preprocessing_version
        )
        ocr_result = process_ocr_document(
            ocr_input, ocr_config, engine=engine, engine_version=engine_version
        )

        normalization_input = build_normalization_input(
            ocr_result=ocr_result, preprocessing_result=preprocessing_result, ocr_config=ocr_config
        )
        normalization_result = normalize_invoice_document(normalization_input, config=normalization_config)

        records[fixture_path.name] = {
            "ingestion_result": ingestion_result,
            "preprocessing_result": preprocessing_result,
            "ocr_result": ocr_result,
            "normalization_input": normalization_input,
            "normalization_result": normalization_result,
        }

    return batch_id, records, normalization_config


def _assert_pipeline_integrity(batch_id, records, normalization_config):
    """SHA-256 continuity, ID stability, evidence isolation and
    persisted/in-memory agreement — checked the same way regardless of
    which OCR provider produced `records`."""

    assert len(records) == 4

    for filename, record in records.items():
        ingestion_result = record["ingestion_result"]
        preprocessing_result = record["preprocessing_result"]
        ocr_result = record["ocr_result"]
        normalization_input = record["normalization_input"]
        normalization_result = record["normalization_result"]

        # --- SHA-256 continuity across every phase boundary -------------
        assert preprocessing_result.source_sha256 == ingestion_result.identity.sha256
        assert ocr_result.source_document_sha256 == preprocessing_result.source_sha256
        assert normalization_input.source_document_sha256 == ocr_result.source_document_sha256

        # --- stable batch and document IDs across every phase -----------
        assert preprocessing_result.batch_id == ingestion_result.identity.batch_id == batch_id
        assert preprocessing_result.document_id == ingestion_result.identity.document_id
        assert ocr_result.batch_id == preprocessing_result.batch_id
        assert ocr_result.document_id == preprocessing_result.document_id
        assert normalization_input.batch_id == ocr_result.batch_id
        assert normalization_input.document_id == ocr_result.document_id
        assert normalization_result.batch_id == ocr_result.batch_id
        assert normalization_result.document_id == ocr_result.document_id

        assert normalization_result.errors == () or normalization_result.status == "FAILED"
        assert normalization_result.invoice_record is not None

        # --- normalization input references the correct OCR result ------
        assert normalization_input.ocr_result is ocr_result

        # --- no candidate or line item crosses document boundaries; every
        #     evidence reference resolves to a token/line from this same
        #     document's own OCR result ---------------------------------
        from ap_agent.tools.normalization import build_ocr_evidence_index

        evidence_index = build_ocr_evidence_index(normalization_input)
        allowed_reference_ids = set(evidence_index.references_by_id)

        invoice_record = normalization_result.invoice_record

        for field in invoice_record.fields:
            for reference in field.evidence_references:
                assert str(reference.reference_id) in allowed_reference_ids

        for line_item in invoice_record.line_items:
            for reference in line_item.evidence_references:
                assert str(reference.reference_id) in allowed_reference_ids

        for candidate in normalization_result.field_candidates:
            for reference in candidate.evidence_references:
                assert str(reference.reference_id) in allowed_reference_ids

        # --- persisted and in-memory Phase 4 results agree ---------------
        document_directory = phase_4_document_directory(normalization_input, config=normalization_config)
        persisted_result = json.loads((document_directory / "normalization_result.json").read_text())
        persisted_invoice = json.loads((document_directory / "normalized_invoice.json").read_text())
        assert persisted_result["status"] == normalization_result.status.value
        assert persisted_result["document_id"] == str(normalization_result.document_id)
        assert persisted_invoice["invoice_record_id"] == str(invoice_record.invoice_record_id)

    # --- no evidence, candidate or line-item ID crosses document boundaries
    document_ids = [str(record["ocr_result"].document_id) for record in records.values()]
    for filename, record in records.items():
        this_document_id = str(record["ocr_result"].document_id)
        document_directory = phase_4_document_directory(record["normalization_input"], config=normalization_config)
        contents = "\n".join(str(p) for p in document_directory.rglob("*"))
        for other_document_id in document_ids:
            if other_document_id != this_document_id:
                assert other_document_id not in contents, (
                    f"{other_document_id} leaked into {filename}'s Phase 4 artifact tree"
                )


@pytest.mark.integration
@pytest.mark.requires_fixtures
@pytest.mark.ocr
@pytest.mark.slow
def test_phase_1_to_4_pipeline_integrity_on_all_four_fixtures(tmp_path):
    """Network-free integrity checks, run with the Tesseract fallback
    forced so this test does not depend on PaddleOCR's model download."""

    batch_id, records, normalization_config = _run_full_pipeline(
        tmp_path, engine=_NeverAvailablePaddleEngine(), engine_version="0.0-disabled"
    )
    _assert_pipeline_integrity(batch_id, records, normalization_config)


@pytest.mark.integration
@pytest.mark.requires_fixtures
@pytest.mark.requires_paddle
@pytest.mark.slow
def test_phase_1_to_4_pipeline_final_statuses_match_the_semantic_golden_baseline(tmp_path):
    """The task-required semantic check: final Phase 4 statuses and header
    field values after the full Phase 1 -> 2 -> 3 -> 4 pipeline must match
    tests/golden/phase_4_expected_results.json, with PaddleOCR as the
    primary provider (not the forced Tesseract path) and no fallback used.
    Requires a real PaddleOCR engine; skipped when the environment cannot
    reach a model-hosting platform (documented blocker,
    docs/m4_phase_3_ocr_report.md)."""

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

    batch_id, records, normalization_config = _run_full_pipeline(
        tmp_path, engine=engine, engine_version=engine_version
    )

    golden_by_filename = {doc["filename"]: doc for doc in golden["documents"]}

    for filename, expected in golden_by_filename.items():
        record = records[filename]
        ocr_result = record["ocr_result"]
        normalization_result = record["normalization_result"]
        invoice_record = normalization_result.invoice_record

        # No Tesseract fallback in the primary parity run.
        assert all(page.ocr_engine == "paddleocr" for page in ocr_result.pages)

        assert normalization_result.status.value == expected["expected_status"]
        assert len(invoice_record.fields) == expected["expected_header_field_count"]
        assert len(invoice_record.line_items) == expected["expected_line_item_count"]

        for field_name_text, expected_raw in expected["expected_fields"].items():
            field_name = InvoiceFieldName[field_name_text]
            normalized_field = invoice_record.get_field(field_name)

            if expected_raw is None:
                assert normalized_field is None, (
                    f"{filename}: expected {field_name_text} to be missing, "
                    f"got {normalized_field.normalized_value if normalized_field else None!r}"
                )
                continue

            actual_value = normalized_field.normalized_value

            if isinstance(actual_value, Decimal):
                assert actual_value == Decimal(expected_raw)
            else:
                assert str(actual_value) == expected_raw

        for reason in expected["required_review_reasons"]:
            assert reason in normalization_result.review_reasons

    # The warped invoice's total must never be inferred.
    warped = records["08181_warped_document_perspective_shadow.jpg"]["normalization_result"]
    warped_total = warped.invoice_record.get_field(InvoiceFieldName.TOTAL_AMOUNT)
    assert warped_total is None

    aggregate = golden["aggregate_expected"]
    all_records = [record["normalization_result"] for record in records.values()]
    assert len(all_records) == aggregate["documents_normalized"]
    assert sum(len(r.invoice_record.fields) for r in all_records) == aggregate["header_fields_total"]
    assert sum(len(r.invoice_record.line_items) for r in all_records) == aggregate["line_items_total"]
    assert sum(1 for r in all_records if r.status == NormalizationStatus.SUCCEEDED) == aggregate["successful_documents"]
    assert (
        sum(1 for r in all_records if r.status == NormalizationStatus.REVIEW_REQUIRED)
        == aggregate["review_required_documents"]
    )
    assert sum(1 for r in all_records if r.status == NormalizationStatus.FAILED) == aggregate["failed_documents"]

    # Deterministic IDs reproduce across a clean rerun of the same inputs.
    rerun_results = {
        filename: normalize_invoice_document(record["normalization_input"], config=normalization_config)
        for filename, record in records.items()
    }
    for filename, record in records.items():
        first_id = record["normalization_result"].invoice_record.invoice_record_id
        second_id = rerun_results[filename].invoice_record.invoice_record_id
        assert first_id == second_id

    _assert_pipeline_integrity(batch_id, records, normalization_config)

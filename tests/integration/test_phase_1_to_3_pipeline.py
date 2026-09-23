"""Bounded Phase 1 -> Phase 2 -> Phase 3 integration test (task M4B §10).

Covers: Phase 1 ingestion -> Phase 2 preprocessing -> Phase 3 OCR -> final
Phase 3 artifact persistence, run against all four controlled fixtures
using temporary artifact roots (no application orchestrator is built here
— that is out of M4 scope).

Verifies:
  - SHA-256 continuity across phase boundaries;
  - batch and document IDs remain stable across all three phases;
  - every Phase 2 page belongs to the correct document;
  - every Phase 3 page consumes the correct Phase 2 artifact;
  - no evidence crosses document boundaries;
  - final statuses match the expected semantic results;
  - persisted and in-memory Phase 3 results agree.

These checks run twice: once with the Tesseract fallback forced (network-
free, always runs), and once with PaddleOCR as the primary provider (marked
`requires_paddle`; skipped only when the environment cannot reach a model-
hosting platform — see docs/m4_phase_3_ocr_report.md).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ap_agent.artifacts.filesystem import calculate_file_sha256
from ap_agent.config.settings import (
    IngestionConfig,
    OCRConfig,
    PaddleEngineOptions,
    PreprocessingConfig,
)
from ap_agent.models.ocr import OCRStatus
from ap_agent.tools.ingestion import create_batch_id, ingest_document
from ap_agent.tools.ocr import build_ocr_document_directory, build_ocr_document_input, process_ocr_document
from ap_agent.tools.preprocessing import build_preprocessing_input, preprocess_document

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "golden" / "phase_3_expected_results.json"


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

        records[fixture_path.name] = {
            "ingestion_result": ingestion_result,
            "preprocessing_result": preprocessing_result,
            "ocr_input": ocr_input,
            "ocr_result": ocr_result,
        }

    return batch_id, records, preprocessing_config, ocr_config


def _assert_pipeline_integrity(batch_id, records, preprocessing_config, ocr_config):
    """SHA-256 continuity, ID stability, page/document association,
    evidence isolation, and persisted/in-memory agreement — checked the
    same way regardless of which OCR provider produced `records`."""

    assert len(records) == 4

    for filename, record in records.items():
        ingestion_result = record["ingestion_result"]
        preprocessing_result = record["preprocessing_result"]
        ocr_input = record["ocr_input"]
        ocr_result = record["ocr_result"]

        # --- SHA-256 continuity across phase boundaries ---------------
        assert preprocessing_result.source_sha256 == ingestion_result.identity.sha256
        assert ocr_input.source_document_sha256 == preprocessing_result.source_sha256

        # Phase 2 re-verified the Phase 1 preserved artifact's bytes
        # directly (not merely trusted the recorded string).
        assert (
            calculate_file_sha256(ingestion_result.stored_path)
            == ingestion_result.identity.sha256
        )

        # --- batch and document IDs remain stable across all 3 phases --
        assert preprocessing_result.batch_id == ingestion_result.identity.batch_id == batch_id
        assert preprocessing_result.document_id == ingestion_result.identity.document_id
        assert ocr_input.batch_id == preprocessing_result.batch_id
        assert ocr_input.document_id == preprocessing_result.document_id
        assert ocr_result.batch_id == preprocessing_result.batch_id
        assert ocr_result.document_id == preprocessing_result.document_id

        # --- every Phase 2 page belongs to the correct document --------
        for page in preprocessing_result.pages:
            assert page.processed_image_path.is_relative_to(
                preprocessing_config.artifact_root
                / str(preprocessing_result.batch_id)
                / str(preprocessing_result.document_id)
            )

        # --- every Phase 3 page consumes the correct Phase 2 artifact ---
        preprocessed_by_number = {p.page_number: p for p in preprocessing_result.pages}
        for ocr_page_input in ocr_input.pages:
            preprocessed_page = preprocessed_by_number[ocr_page_input.page_number]
            assert ocr_page_input.processed_image_path == preprocessed_page.processed_image_path
            assert (
                ocr_page_input.processed_image_sha256
                == preprocessed_page.processed_image_sha256
            )

        for ocr_page_result in ocr_result.pages:
            assert (
                calculate_file_sha256(ocr_page_result.processed_image_path)
                == ocr_page_result.processed_image_sha256
            )

        # --- persisted and in-memory Phase 3 results agree --------------
        document_directory = build_ocr_document_directory(ocr_input, ocr_config)
        persisted_document = json.loads(
            (document_directory / "ocr_document_result.json").read_text()
        )
        persisted_event = json.loads((document_directory / "ocr_event.json").read_text())
        assert persisted_document["status"] == ocr_result.status.value
        assert persisted_event["status"] == ocr_result.event.status.value
        assert persisted_event["review_required"] == ocr_result.event.review_required
        for page_result in ocr_result.pages:
            page_directory = document_directory / f"page_{page_result.page_number:03d}"
            persisted_page = json.loads((page_directory / "ocr_page_result.json").read_text())
            assert persisted_page["status"] == page_result.status.value
            assert persisted_page["review_reasons"] == list(page_result.review_reasons)

    # --- no evidence crosses document boundaries -----------------------
    document_ids = [str(record["ocr_result"].document_id) for record in records.values()]
    for filename, record in records.items():
        this_document_id = str(record["ocr_result"].document_id)
        document_directory = build_ocr_document_directory(record["ocr_input"], ocr_config)
        contents = "\n".join(str(p) for p in document_directory.rglob("*"))
        for other_document_id in document_ids:
            if other_document_id != this_document_id:
                assert other_document_id not in contents, (
                    f"{other_document_id} leaked into {filename}'s artifact tree"
                )


@pytest.mark.integration
@pytest.mark.requires_fixtures
@pytest.mark.ocr
@pytest.mark.slow
def test_phase_1_to_3_pipeline_integrity_on_all_four_fixtures(tmp_path):
    """Network-free integrity checks (SHA-256 continuity, ID stability,
    page/document association, evidence isolation, persisted/in-memory
    agreement), run with the Tesseract fallback forced so this test does
    not depend on PaddleOCR's model download."""

    batch_id, records, preprocessing_config, ocr_config = _run_full_pipeline(
        tmp_path, engine=_NeverAvailablePaddleEngine(), engine_version="0.0-disabled"
    )
    _assert_pipeline_integrity(batch_id, records, preprocessing_config, ocr_config)


@pytest.mark.integration
@pytest.mark.requires_fixtures
@pytest.mark.requires_paddle
@pytest.mark.slow
def test_phase_1_to_3_pipeline_final_statuses_match_the_semantic_golden_baseline(tmp_path):
    """The task-required semantic check: final statuses after the full
    Phase 1 -> Phase 2 -> Phase 3 pipeline must match
    tests/golden/phase_3_expected_results.json, and the same SHA-256
    continuity / ID stability / evidence isolation / persisted-in-memory
    agreement checks as the Tesseract-forced integrity test must hold with
    PaddleOCR as the primary provider (not the forced Tesseract path).
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

    batch_id, records, preprocessing_config, ocr_config = _run_full_pipeline(
        tmp_path, engine=engine, engine_version=engine_version
    )

    for expected in golden["documents"]:
        ocr_result = records[expected["filename"]]["ocr_result"]
        assert ocr_result.status.value == expected["expected_status"]

    _assert_pipeline_integrity(batch_id, records, preprocessing_config, ocr_config)

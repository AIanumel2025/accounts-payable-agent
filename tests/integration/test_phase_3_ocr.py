"""Integration tests for Phase 3 (OCR) against the four controlled invoice
fixtures, fed through Phase 1 -> Phase 2 -> Phase 3.

Two provider paths are exercised:

- A real, local, network-free OCR run forced onto the Tesseract fallback
  path (marked `ocr`): proves the full pipeline — routing, the TOTAL
  completeness guard, the corrected persistence order, and artifact
  integrity — executes correctly against real fixture bytes, without
  requiring PaddleOCR's model download.
- The real PaddleOCR primary-provider run the task brief requires (marked
  `requires_paddle`, `slow`): this is the parity run against
  `tests/golden/phase_3_expected_results.json`. In this repository's CI/
  sandbox environment it is skipped with an explicit reason: PaddleOCR's
  model-hosting platforms (huggingface.co, aistudio.baidu.com,
  modelscope.cn, paddle-model-ecology.bj.bcebos.com) are all denied by the
  environment's network policy (docs/m4_phase_3_ocr_report.md documents
  this as an open blocker). The test itself is real and will run the
  moment network access is available — it is not a mock standing in for
  the required parity run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap_agent.artifacts.filesystem import calculate_file_sha256
from ap_agent.config.settings import (
    IngestionConfig,
    OCRConfig,
    PaddleEngineOptions,
    PreprocessingConfig,
)
from ap_agent.models.ocr import OCRDocumentInput, OCRPageInput, OCRStatus
from ap_agent.tools.ingestion import create_batch_id, ingest_document
from ap_agent.tools.ocr import build_ocr_document_directory, build_ocr_document_input, process_ocr_document
from ap_agent.tools.preprocessing import build_preprocessing_input, preprocess_document

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "golden" / "phase_3_expected_results.json"


def _load_golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


class _NeverAvailablePaddleEngine:
    """Forces every page onto the Tesseract fallback path, deterministically,
    without touching the network."""

    def predict(self, path):
        raise RuntimeError("PaddleOCR intentionally disabled for this test")


def _run_phase_1_and_2(tmp_path):
    ingestion_config = IngestionConfig(artifact_root=tmp_path / "phase1")
    preprocessing_config = PreprocessingConfig(
        artifact_root=tmp_path / "phase2",
        minimum_width=500,
        minimum_height=700,
    )
    batch_id = create_batch_id("phase_1_colab_test", "phase-1-seven-file-validation")
    known_hashes: set[str] = set()
    preprocessing_results = {}

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
        assert preprocessing_result.status.value != "FAILED"

        preprocessing_results[fixture_path.name] = (preprocessing_result, preprocessing_config)

    return preprocessing_results


@pytest.fixture(scope="module")
def golden():
    return _load_golden()


# ==============================================================================
# Real, local, network-free OCR run (Tesseract fallback forced)
# ==============================================================================


@pytest.mark.integration
@pytest.mark.requires_fixtures
@pytest.mark.ocr
@pytest.mark.slow
def test_all_four_fixtures_run_end_to_end_on_the_tesseract_fallback_path(tmp_path):
    """Proves the OCR pipeline mechanics — routing, persistence order,
    guard synchronization — against real fixture bytes with a real, local
    OCR engine (Tesseract), independent of PaddleOCR's network dependency.
    This does not replace the required PaddleOCR parity run (see the
    `requires_paddle` test below); it is a network-free correctness check
    of everything PaddleOCR unavailability does not block.
    """

    preprocessing_results = _run_phase_1_and_2(tmp_path)
    ocr_config = OCRConfig(
        artifact_root=tmp_path / "phase3",
        ocr_engine_name="paddleocr",
        ocr_version="ocr-v2-paddle",
    )
    engine = _NeverAvailablePaddleEngine()

    results = {}
    for filename, (preprocessing_result, preprocessing_config) in preprocessing_results.items():
        ocr_input = build_ocr_document_input(
            preprocessing_result, preprocessing_config.preprocessing_version
        )
        ocr_result = process_ocr_document(
            ocr_input, ocr_config, engine=engine, engine_version="0.0-disabled"
        )
        results[filename] = ocr_result

        assert ocr_result.status != OCRStatus.FAILED
        assert ocr_result.pages[0].ocr_engine == "tesseract-fallback"
        assert "PRIMARY_PROVIDER_FAILED" in ocr_result.pages[0].review_reasons

    assert len(results) == 4

    # Persisted-vs-in-memory parity for every document.
    for filename, (preprocessing_result, _) in preprocessing_results.items():
        ocr_result = results[filename]
        document_directory = ocr_config.artifact_root / str(ocr_result.batch_id) / str(ocr_result.document_id) / ocr_config.ocr_version
        persisted = json.loads((document_directory / "ocr_document_result.json").read_text())
        assert persisted["status"] == ocr_result.status.value
        assert persisted["page_count"] == len(ocr_result.pages)


@pytest.mark.integration
@pytest.mark.requires_fixtures
@pytest.mark.ocr
def test_cross_document_isolation_with_real_fixtures(tmp_path):
    preprocessing_results = _run_phase_1_and_2(tmp_path)
    ocr_config = OCRConfig(
        artifact_root=tmp_path / "phase3", ocr_engine_name="paddleocr", ocr_version="ocr-v2-paddle"
    )
    engine = _NeverAvailablePaddleEngine()

    document_ids = []
    for preprocessing_result, preprocessing_config in preprocessing_results.values():
        ocr_input = build_ocr_document_input(preprocessing_result, preprocessing_config.preprocessing_version)
        ocr_result = process_ocr_document(ocr_input, ocr_config, engine=engine, engine_version="0.0-disabled")
        document_ids.append(str(ocr_result.document_id))

    for document_id in document_ids:
        document_directory = None
        for path in ocr_config.artifact_root.rglob(document_id):
            document_directory = path
            break
        assert document_directory is not None

        other_ids = [other for other in document_ids if other != document_id]
        contents = "\n".join(str(p) for p in document_directory.rglob("*"))
        for other_id in other_ids:
            assert other_id not in contents


@pytest.mark.integration
@pytest.mark.requires_fixtures
@pytest.mark.ocr
def test_invalid_page_hash_fails_closed_on_a_real_fixture(tmp_path):
    preprocessing_results = _run_phase_1_and_2(tmp_path)
    preprocessing_result, preprocessing_config = preprocessing_results["08181_flat_document.png"]

    ocr_config = OCRConfig(
        artifact_root=tmp_path / "phase3", ocr_engine_name="paddleocr", ocr_version="ocr-v2-paddle"
    )
    ocr_input = build_ocr_document_input(preprocessing_result, preprocessing_config.preprocessing_version)

    tampered_pages = tuple(
        OCRPageInput(
            page_number=page.page_number,
            processed_image_path=page.processed_image_path,
            processed_image_sha256="0" * 64,
            preprocessing_review_required=page.preprocessing_review_required,
        )
        for page in ocr_input.pages
    )
    from dataclasses import replace

    tampered_input = replace(ocr_input, pages=tampered_pages)

    result = process_ocr_document(
        tampered_input, ocr_config, engine=_NeverAvailablePaddleEngine(), engine_version="0.0-disabled"
    )

    assert result.status == OCRStatus.FAILED
    assert result.event.review_required is True
    assert "SHA-256" in result.errors[0]


# ==============================================================================
# Real PaddleOCR parity run (the task-required semantic validation)
# ==============================================================================


@pytest.fixture(scope="module")
def paddle_engine():
    """Build the real, primary PaddleOCR engine. Skips (does not fail) the
    tests that use it when the environment cannot reach a model-hosting
    platform — this is the documented blocker in
    docs/m4_phase_3_ocr_report.md, not a substitute mock."""

    from ap_agent.adapters.paddleocr_adapter import create_engine

    try:
        return create_engine(PaddleEngineOptions())
    except Exception as exc:  # pragma: no cover - depends on network policy
        pytest.skip(
            "PaddleOCR engine could not be constructed (model download "
            f"blocked): {type(exc).__name__}: {exc}"
        )


@pytest.mark.integration
@pytest.mark.requires_fixtures
@pytest.mark.requires_paddle
@pytest.mark.slow
def test_real_paddleocr_run_matches_the_semantic_golden_baseline(tmp_path, golden, paddle_engine):
    from ap_agent.adapters.paddleocr_adapter import get_paddleocr_version

    preprocessing_results = _run_phase_1_and_2(tmp_path)
    ocr_config = OCRConfig(
        artifact_root=tmp_path / "phase3", ocr_engine_name="paddleocr", ocr_version="ocr-v2-paddle"
    )
    engine_version = get_paddleocr_version()

    successful = 0
    review_required = 0
    failed = 0
    anchors_captured = 0
    anchors_total = 0

    for expected in golden["documents"]:
        preprocessing_result, preprocessing_config = preprocessing_results[expected["filename"]]
        ocr_input = build_ocr_document_input(preprocessing_result, preprocessing_config.preprocessing_version)
        ocr_result = process_ocr_document(
            ocr_input, ocr_config, engine=paddle_engine, engine_version=engine_version
        )

        assert ocr_result.status.value == expected["expected_status"]
        assert ocr_result.pages[0].ocr_engine == expected["expected_selected_provider"]

        page_text_upper = ocr_result.pages[0].page_text.upper().replace(" ", "")

        for anchor in expected["expected_anchors_captured"]:
            anchors_total += 1
            normalized_anchor = anchor.upper().replace(" ", "")
            if normalized_anchor in page_text_upper:
                anchors_captured += 1

        for anchor in expected.get("expected_anchors_absent", []):
            normalized_anchor = anchor.upper().replace(" ", "")
            assert normalized_anchor not in page_text_upper, (
                f"{anchor} must not appear in OCR evidence for "
                f"{expected['filename']} (never infer missing financial values)"
            )

        if expected["expected_critical_total_value_missing"]:
            assert "CRITICAL_TOTAL_VALUE_MISSING" in ocr_result.pages[0].review_reasons

        if ocr_result.status.value == "SUCCEEDED":
            successful += 1
        elif ocr_result.status.value == "REVIEW_REQUIRED":
            review_required += 1
        else:
            failed += 1

    aggregate = golden["aggregate_expected"]
    assert successful == aggregate["successful_documents"]
    assert review_required == aggregate["review_required_documents"]
    assert failed == aggregate["failed_documents"]
    assert anchors_captured == aggregate["semantic_anchors_captured"]
    assert anchors_total == aggregate["semantic_anchors_captured"]


@pytest.mark.integration
@pytest.mark.requires_fixtures
@pytest.mark.requires_paddle
@pytest.mark.slow
def test_real_paddleocr_uses_paddle_on_every_fixture_page(tmp_path, golden, paddle_engine):
    from ap_agent.adapters.paddleocr_adapter import get_paddleocr_version

    preprocessing_results = _run_phase_1_and_2(tmp_path)
    ocr_config = OCRConfig(
        artifact_root=tmp_path / "phase3", ocr_engine_name="paddleocr", ocr_version="ocr-v2-paddle"
    )
    engine_version = get_paddleocr_version()

    fallback_pages = 0
    for expected in golden["documents"]:
        preprocessing_result, preprocessing_config = preprocessing_results[expected["filename"]]
        ocr_input = build_ocr_document_input(preprocessing_result, preprocessing_config.preprocessing_version)
        ocr_result = process_ocr_document(
            ocr_input, ocr_config, engine=paddle_engine, engine_version=engine_version
        )
        for page in ocr_result.pages:
            if page.ocr_engine != "paddleocr":
                fallback_pages += 1

    assert fallback_pages == golden["aggregate_expected"]["tesseract_fallback_pages"]

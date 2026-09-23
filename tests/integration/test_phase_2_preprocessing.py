"""Integration tests for Phase 2 (preprocessing) against the four
controlled invoice fixtures, fed through the Phase 1 -> Phase 2 bridge
(decision D-6), plus multi-document and multi-page batch behaviour using
generated documents.

Parity source: `tests/golden/phase_2_expected_results.json`, cross-checked
against `docs/modularisation_map.md` §10.2.
"""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.artifacts.filesystem import calculate_file_sha256
from ap_agent.config.settings import IngestionConfig, PreprocessingConfig
from ap_agent.tools.ingestion import create_batch_id, ingest_document
from ap_agent.tools.preprocessing import build_preprocessing_input, preprocess_document

pytestmark = [pytest.mark.integration, pytest.mark.requires_fixtures]

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "golden" / "phase_2_expected_results.json"


def _load_golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _preprocessing_config(tmp_path, golden):
    cfg = golden["config"]
    return PreprocessingConfig(
        artifact_root=tmp_path / "phase2",
        pdf_dpi=cfg["pdf_dpi"],
        enable_deskew=cfg["enable_deskew"],
        enable_clahe=cfg["enable_clahe"],
        enable_denoising=cfg["enable_denoising"],
        minimum_width=cfg["minimum_width"],
        minimum_height=cfg["minimum_height"],
        maximum_brightness=cfg["maximum_brightness"],
    )


def _ingest_all_fixtures(tmp_path):
    ingestion_config = IngestionConfig(artifact_root=tmp_path / "phase1")
    batch_id = create_batch_id("phase_1_colab_test", "phase-1-seven-file-validation")
    known_hashes: set[str] = set()
    results = {}
    for fixture_path in sorted(FIXTURES_DIR.iterdir()):
        if fixture_path.name == "manifest.json" or not fixture_path.is_file():
            continue
        result = ingest_document(
            file_path=fixture_path,
            config=ingestion_config,
            batch_id=batch_id,
            ingestion_source="phase_1_colab_test",
            known_sha256_values=known_hashes,
        )
        known_hashes.add(result.identity.sha256)
        results[fixture_path.name] = result
    return batch_id, results


@pytest.fixture(scope="module")
def golden():
    return _load_golden()


def test_all_four_fixtures_succeed_with_one_page_each(tmp_path, golden):
    _, ingestion_results = _ingest_all_fixtures(tmp_path)
    config = _preprocessing_config(tmp_path, golden)

    succeeded = 0
    for expected in golden["documents"]:
        result = ingestion_results[expected["filename"]]
        preprocessing_input = build_preprocessing_input(result)
        preprocessing_result = preprocess_document(preprocessing_input, config)

        assert preprocessing_result.status.value == expected["expected_status"]
        assert len(preprocessing_result.pages) == expected["expected_page_count"]
        assert preprocessing_result.event.review_required == expected["expected_review_required"]
        succeeded += preprocessing_result.status.value == "SUCCEEDED"

    assert succeeded == 4


def test_fixture_page_quality_matches_the_golden_baseline(tmp_path, golden):
    _, ingestion_results = _ingest_all_fixtures(tmp_path)
    config = _preprocessing_config(tmp_path, golden)
    tolerance = 0.01

    for expected in golden["documents"]:
        result = ingestion_results[expected["filename"]]
        preprocessing_input = build_preprocessing_input(result)
        preprocessing_result = preprocess_document(preprocessing_input, config)
        page = preprocessing_result.pages[0]
        expected_page = expected["page_1"]

        # Width/height/flags are Tier X (exact) in any environment.
        assert page.quality.width == expected_page["width"]
        assert page.quality.height == expected_page["height"]
        assert list(page.quality.quality_flags) == expected_page["quality_flags"]

        # Metrics derived from pixel math: exact in the pinned CI
        # environment, tolerant elsewhere (R-06).
        assert page.quality.brightness_score == pytest.approx(
            expected_page["brightness_score"], abs=tolerance
        )
        assert page.quality.contrast_score == pytest.approx(
            expected_page["contrast_score"], abs=tolerance
        )
        assert page.quality.blur_score == pytest.approx(
            expected_page["blur_score"], abs=1.0
        )
        assert page.quality.detected_skew_angle == pytest.approx(
            expected_page["detected_skew_angle"], abs=tolerance
        )


def test_fixture_document_and_batch_ids_match_phase_1(tmp_path, golden):
    batch_id, ingestion_results = _ingest_all_fixtures(tmp_path)
    config = _preprocessing_config(tmp_path, golden)

    assert str(batch_id) == golden["batch"]["expected_batch_id"]

    for expected in golden["documents"]:
        result = ingestion_results[expected["filename"]]
        preprocessing_input = build_preprocessing_input(result)
        preprocessing_result = preprocess_document(preprocessing_input, config)

        assert str(preprocessing_result.document_id) == expected["expected_document_id"]
        assert str(preprocessing_result.batch_id) == golden["batch"]["expected_batch_id"]


def test_fixture_artifacts_land_at_the_expected_relative_directory(tmp_path, golden):
    _, ingestion_results = _ingest_all_fixtures(tmp_path)
    config = _preprocessing_config(tmp_path, golden)

    for expected in golden["documents"]:
        result = ingestion_results[expected["filename"]]
        preprocessing_input = build_preprocessing_input(result)
        preprocess_document(preprocessing_input, config)

        expected_directory = config.artifact_root / expected["expected_artifact_relative_directory"]
        assert expected_directory.is_dir()
        assert (expected_directory / "preprocessing_result.json").exists()
        assert (expected_directory / "preprocessing_event.json").exists()


def test_phase_2_reads_the_phase_1_preserved_artifact_not_the_fixture_path(tmp_path, golden):
    """The bridge must consume Phase 1's content-addressed artifact
    (`originals/<content_id>/original.<ext>`), never the fixture path
    under `tests/fixtures/invoices/` directly."""
    _, ingestion_results = _ingest_all_fixtures(tmp_path)
    config = _preprocessing_config(tmp_path, golden)

    for expected in golden["documents"]:
        result = ingestion_results[expected["filename"]]
        preprocessing_input = build_preprocessing_input(result)

        assert "originals" in preprocessing_input.source_path.parts
        assert str(FIXTURES_DIR) not in str(preprocessing_input.source_path)

        preprocessing_result = preprocess_document(preprocessing_input, config)
        assert preprocessing_result.status.value == "SUCCEEDED"


def test_phase_2_fails_closed_on_a_tampered_preserved_artifact(tmp_path, golden):
    _, ingestion_results = _ingest_all_fixtures(tmp_path)
    config = _preprocessing_config(tmp_path, golden)

    result = ingestion_results["08181_flat_document.png"]
    preprocessing_input = build_preprocessing_input(result)

    # Tamper with the preserved artifact after Phase 1 wrote it.
    preprocessing_input.source_path.write_bytes(b"corrupted bytes")

    preprocessing_result = preprocess_document(preprocessing_input, config)

    assert preprocessing_result.status.value == "FAILED"
    assert "sha-256" in preprocessing_result.errors[0].lower()
    assert preprocessing_result.pages == ()


def test_no_source_file_is_modified_by_preprocessing(tmp_path, golden):
    _, ingestion_results = _ingest_all_fixtures(tmp_path)
    config = _preprocessing_config(tmp_path, golden)

    fixture_hashes_before = {
        path.name: calculate_file_sha256(path)
        for path in FIXTURES_DIR.iterdir()
        if path.is_file() and path.name != "manifest.json"
    }

    for expected in golden["documents"]:
        result = ingestion_results[expected["filename"]]
        preprocessing_input = build_preprocessing_input(result)
        preprocess_document(preprocessing_input, config)

    fixture_hashes_after = {
        path.name: calculate_file_sha256(path)
        for path in FIXTURES_DIR.iterdir()
        if path.is_file() and path.name != "manifest.json"
    }

    assert fixture_hashes_before == fixture_hashes_after


def test_cross_document_isolation_across_all_four_fixtures(tmp_path, golden):
    _, ingestion_results = _ingest_all_fixtures(tmp_path)
    config = _preprocessing_config(tmp_path, golden)

    document_ids = []
    for expected in golden["documents"]:
        result = ingestion_results[expected["filename"]]
        preprocessing_input = build_preprocessing_input(result)
        preprocess_document(preprocessing_input, config)
        document_ids.append(str(preprocessing_input.document_id))

    for document_id in document_ids:
        document_directory = None
        for path in config.artifact_root.rglob(document_id):
            document_directory = path
            break
        assert document_directory is not None

        other_ids = [other for other in document_ids if other != document_id]
        contents = "\n".join(str(p) for p in document_directory.rglob("*"))
        for other_id in other_ids:
            assert other_id not in contents


def test_repeated_preprocessing_of_the_same_document_is_idempotent(tmp_path, golden):
    _, ingestion_results = _ingest_all_fixtures(tmp_path)
    config = _preprocessing_config(tmp_path, golden)

    result = ingestion_results["invoice_Aaron Bergman_36258.pdf"]
    preprocessing_input = build_preprocessing_input(result)

    first = preprocess_document(preprocessing_input, config)
    second = preprocess_document(preprocessing_input, config)

    assert first.status == second.status
    assert first.pages[0].processed_image_sha256 == second.pages[0].processed_image_sha256


# --- generated documents: multipage PDF and a batch of more than 4 ---------


def _make_pdf(path: Path, page_count: int, width=550, height=750):
    import pymupdf

    document = pymupdf.open()
    for index in range(page_count):
        page = document.new_page(width=width, height=height)
        page.insert_text((72, 72), f"Invoice page {index + 1} of {page_count}")
        page.insert_text((72, 120), "INVOICE TOTAL: 123.45")
    document.save(str(path))
    document.close()


@pytest.mark.slow
def test_multipage_pdf_produces_one_preprocessed_page_per_pdf_page(tmp_path):
    pdf_path = tmp_path / "source" / "multi_invoice.pdf"
    pdf_path.parent.mkdir(parents=True)
    _make_pdf(pdf_path, page_count=5)

    ingestion_config = IngestionConfig(artifact_root=tmp_path / "phase1")
    batch_id = create_batch_id("generated-batch", "multipage-pdf")
    result = ingest_document(
        file_path=pdf_path,
        config=ingestion_config,
        batch_id=batch_id,
        ingestion_source="generated-batch",
        known_sha256_values=set(),
    )

    preprocessing_config = PreprocessingConfig(
        artifact_root=tmp_path / "phase2",
        pdf_dpi=150,
        minimum_width=500,
        minimum_height=700,
    )
    preprocessing_input = build_preprocessing_input(result)
    preprocessing_result = preprocess_document(preprocessing_input, preprocessing_config)

    assert len(preprocessing_result.pages) == 5
    assert [page.page_number for page in preprocessing_result.pages] == [1, 2, 3, 4, 5]

    page_numbers_seen = set()
    for page in preprocessing_result.pages:
        assert page.page_number not in page_numbers_seen
        page_numbers_seen.add(page.page_number)
        assert page.processed_image_path.exists()
        assert page.original_render_path.exists()


@pytest.mark.slow
def test_batch_of_more_than_four_generated_documents_are_all_processed(tmp_path):
    """D-2: runtime code must not assume there are exactly four documents."""
    ingestion_config = IngestionConfig(artifact_root=tmp_path / "phase1")
    preprocessing_config = PreprocessingConfig(
        artifact_root=tmp_path / "phase2",
        pdf_dpi=100,
        minimum_width=500,
        minimum_height=700,
    )
    batch_id = create_batch_id("generated-batch", "more-than-four")

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    document_count = 9
    known_hashes: set[str] = set()
    processed_document_ids = []

    for index in range(document_count):
        pdf_path = source_dir / f"invoice_{index:02d}.pdf"
        _make_pdf(pdf_path, page_count=1)

        ingestion_result = ingest_document(
            file_path=pdf_path,
            config=ingestion_config,
            batch_id=batch_id,
            ingestion_source="generated-batch",
            known_sha256_values=known_hashes,
        )
        known_hashes.add(ingestion_result.identity.sha256)

        preprocessing_input = build_preprocessing_input(ingestion_result)
        preprocessing_result = preprocess_document(preprocessing_input, preprocessing_config)

        # A sparse, mostly-blank generated page may legitimately trip a
        # quality flag (e.g. POSSIBLE_BLUR); the point of this test is that
        # every one of the 9 documents is processed and persisted
        # independently, not that every synthetic page is pristine.
        assert preprocessing_result.status.value != "FAILED"
        assert len(preprocessing_result.pages) == 1
        processed_document_ids.append(preprocessing_result.document_id)

    assert len(processed_document_ids) == document_count
    assert len(set(processed_document_ids)) == document_count

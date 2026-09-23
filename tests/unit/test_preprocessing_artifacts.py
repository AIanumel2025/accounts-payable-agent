"""Unit tests for Phase 2 artifact writers (`ap_agent.artifacts.filesystem`,
`ap_agent.artifacts.serialization`) and the `preprocess_document`
orchestrator's failure-closed and persistence behaviour (notebook cells 26,
28). All tests use synthetic images and `tmp_path`.
"""

import json
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from ap_agent.artifacts.filesystem import (
    calculate_file_sha256,
    save_png_atomically,
    write_json_atomically,
)
from ap_agent.artifacts.serialization import (
    page_quality_to_dict,
    preprocessed_page_to_dict,
    preprocessing_event_to_dict,
)
from ap_agent.config.settings import PreprocessingConfig
from ap_agent.models.preprocessing import (
    PageQuality,
    PreprocessedPage,
    PreprocessingEvent,
    PreprocessingInput,
    PreprocessingStatus,
)
from ap_agent.tools.preprocessing import preprocess_document

pytestmark = pytest.mark.unit


def _white_page(width=900, height=1200):
    return np.full((height, width, 3), 250, dtype=np.uint8)


def _draw_text_block(image, box=(100, 100, 400, 60)):
    x, y, w, h = box
    image[y : y + h, x : x + w] = 20
    return image


def _clean_page():
    return _draw_text_block(_white_page())


# --- calculate_file_sha256 ---------------------------------------------------


def test_calculate_file_sha256_matches_hashlib(tmp_path):
    import hashlib

    path = tmp_path / "data.bin"
    payload = b"some file bytes" * 1000
    path.write_bytes(payload)

    assert calculate_file_sha256(path) == hashlib.sha256(payload).hexdigest()


def test_calculate_file_sha256_reads_in_chunks_larger_than_one_block(tmp_path):
    import hashlib

    path = tmp_path / "large.bin"
    payload = b"x" * (1024 * 1024 * 3 + 17)
    path.write_bytes(payload)

    assert calculate_file_sha256(path) == hashlib.sha256(payload).hexdigest()


# --- write_json_atomically (Phase 2/3 byte format, cell 26) -----------------


def test_write_json_atomically_writes_indented_ascii_escaped_json(tmp_path):
    destination = tmp_path / "nested" / "result.json"
    write_json_atomically(destination, {"currency": "€", "value": 1})

    raw = destination.read_text(encoding="utf-8")
    assert raw == json.dumps({"currency": "€", "value": 1}, indent=2, default=str)
    assert "\\u20ac" in raw  # ASCII-escaped, not a literal euro sign.


def test_write_json_atomically_creates_parent_directories(tmp_path):
    destination = tmp_path / "a" / "b" / "c" / "result.json"
    write_json_atomically(destination, {"ok": True})
    assert destination.exists()


def test_write_json_atomically_leaves_no_temp_file_behind(tmp_path):
    destination = tmp_path / "result.json"
    write_json_atomically(destination, {"ok": True})

    remaining = list(tmp_path.iterdir())
    assert remaining == [destination]


def test_write_json_atomically_uses_default_str_for_unknown_types(tmp_path):
    from datetime import datetime, timezone

    destination = tmp_path / "result.json"
    timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    write_json_atomically(destination, {"occurred_at": timestamp})

    loaded = json.loads(destination.read_text(encoding="utf-8"))
    assert loaded["occurred_at"] == str(timestamp)


# --- save_png_atomically -----------------------------------------------------


def test_save_png_atomically_writes_a_readable_png(tmp_path):
    import cv2

    destination = tmp_path / "out.png"
    image = _white_page()
    save_png_atomically(image, destination)

    reloaded = cv2.imread(str(destination))
    assert reloaded.shape == image.shape


def test_save_png_atomically_leaves_no_temp_file_behind(tmp_path):
    destination = tmp_path / "out.png"
    save_png_atomically(_white_page(), destination)

    remaining = list(tmp_path.iterdir())
    assert remaining == [destination]


def test_save_png_atomically_creates_parent_directories(tmp_path):
    destination = tmp_path / "nested" / "dir" / "out.png"
    save_png_atomically(_white_page(), destination)
    assert destination.exists()


# --- serialisers --------------------------------------------------------------


def test_page_quality_to_dict_uses_a_list_for_quality_flags():
    quality = PageQuality(
        page_number=1,
        width=100,
        height=200,
        brightness_score=1.0,
        contrast_score=2.0,
        blur_score=3.0,
        detected_skew_angle=0.0,
        quality_flags=("LOW_CONTRAST", "POSSIBLE_BLUR"),
        review_required=True,
    )
    payload = page_quality_to_dict(quality)
    assert payload["quality_flags"] == ["LOW_CONTRAST", "POSSIBLE_BLUR"]
    assert isinstance(payload["quality_flags"], list)


def test_preprocessing_event_to_dict_serialises_enum_and_timestamp():
    from datetime import datetime, timezone

    event = PreprocessingEvent(
        event_type="PREPROCESSING",
        status=PreprocessingStatus.SUCCEEDED,
        batch_id=uuid4(),
        document_id=uuid4(),
        occurred_at=datetime(2024, 5, 1, tzinfo=timezone.utc),
        message="ok",
        review_required=False,
    )
    payload = preprocessing_event_to_dict(event)
    assert payload["status"] == "SUCCEEDED"
    assert payload["occurred_at"] == "2024-05-01T00:00:00+00:00"
    assert payload["batch_id"] == str(event.batch_id)


def test_preprocessed_page_to_dict_embeds_the_quality_payload():
    quality = PageQuality(
        page_number=1,
        width=1,
        height=1,
        brightness_score=1.0,
        contrast_score=1.0,
        blur_score=1.0,
        detected_skew_angle=0.0,
        quality_flags=(),
        review_required=False,
    )
    page = PreprocessedPage(
        page_number=1,
        original_render_path=Path("pages/page_001/original_render.png"),
        processed_image_path=Path("pages/page_001/processed.png"),
        original_render_sha256="a" * 64,
        processed_image_sha256="b" * 64,
        quality=quality,
    )
    payload = preprocessed_page_to_dict(page)
    assert payload["quality"] == page_quality_to_dict(quality)
    assert payload["original_render_path"] == str(page.original_render_path)


# --- preprocess_document: fail-closed and persistence -----------------------


def _config(tmp_path, **overrides):
    return PreprocessingConfig(
        artifact_root=tmp_path / "artifacts",
        minimum_width=500,
        minimum_height=700,
        **overrides,
    )


def _write_source_image(tmp_path, name="page.png", image=None):
    import cv2

    path = tmp_path / name
    cv2.imwrite(str(path), image if image is not None else _clean_page())
    return path


def test_preprocess_document_fails_closed_on_missing_source(tmp_path):
    config = _config(tmp_path)
    preprocessing_input = PreprocessingInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_path=tmp_path / "missing.png",
        source_sha256="a" * 64,
    )

    result = preprocess_document(preprocessing_input, config)

    assert result.status == PreprocessingStatus.FAILED
    assert result.pages == ()
    assert result.event.review_required is True
    assert "not found" in result.errors[0].lower()


def test_preprocess_document_fails_closed_on_sha256_mismatch(tmp_path):
    source_path = _write_source_image(tmp_path)
    config = _config(tmp_path)
    preprocessing_input = PreprocessingInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_path=source_path,
        source_sha256="0" * 64,  # deliberately wrong
    )

    result = preprocess_document(preprocessing_input, config)

    assert result.status == PreprocessingStatus.FAILED
    assert "sha-256" in result.errors[0].lower()


def test_preprocess_document_never_uses_an_unverified_source(tmp_path):
    """The hash check happens before any page is loaded or written."""
    source_path = _write_source_image(tmp_path)
    config = _config(tmp_path)
    preprocessing_input = PreprocessingInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_path=source_path,
        source_sha256="0" * 64,
    )

    result = preprocess_document(preprocessing_input, config)

    assert result.pages == ()
    pages_dir = (
        config.artifact_root
        / str(preprocessing_input.batch_id)
        / str(preprocessing_input.document_id)
        / config.preprocessing_version
        / "pages"
    )
    assert not pages_dir.exists()


def test_preprocess_document_fails_closed_on_corrupted_document(tmp_path):
    corrupted_path = tmp_path / "corrupted.png"
    corrupted_path.write_bytes(b"not a real png")
    config = _config(tmp_path)
    preprocessing_input = PreprocessingInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_path=corrupted_path,
        source_sha256=calculate_file_sha256(corrupted_path),
    )

    result = preprocess_document(preprocessing_input, config)

    assert result.status == PreprocessingStatus.FAILED


def test_preprocess_document_fails_closed_on_unsupported_extension(tmp_path):
    bad_path = tmp_path / "notes.txt"
    bad_path.write_text("hello")
    config = _config(tmp_path)
    preprocessing_input = PreprocessingInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_path=bad_path,
        source_sha256=calculate_file_sha256(bad_path),
    )

    result = preprocess_document(preprocessing_input, config)

    assert result.status == PreprocessingStatus.FAILED
    assert "unsupported" in result.errors[0].lower()


def test_preprocess_document_succeeds_on_a_clean_synthetic_page(tmp_path):
    source_path = _write_source_image(tmp_path)
    config = _config(tmp_path)
    preprocessing_input = PreprocessingInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_path=source_path,
        source_sha256=calculate_file_sha256(source_path),
    )

    result = preprocess_document(preprocessing_input, config)

    assert result.status == PreprocessingStatus.SUCCEEDED
    assert len(result.pages) == 1
    assert result.event.review_required is False


def test_preprocess_document_persists_result_and_event_artifacts(tmp_path):
    source_path = _write_source_image(tmp_path)
    config = _config(tmp_path)
    preprocessing_input = PreprocessingInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_path=source_path,
        source_sha256=calculate_file_sha256(source_path),
    )

    result = preprocess_document(preprocessing_input, config)

    document_directory = (
        config.artifact_root
        / str(preprocessing_input.batch_id)
        / str(preprocessing_input.document_id)
        / config.preprocessing_version
    )
    result_path = document_directory / "preprocessing_result.json"
    event_path = document_directory / "preprocessing_event.json"
    assert result_path.exists()
    assert event_path.exists()

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["status"] == "SUCCEEDED"
    assert payload["page_count"] == 1

    page_directory = document_directory / "pages" / "page_001"
    assert (page_directory / "original_render.png").exists()
    assert (page_directory / "processed.png").exists()
    assert (page_directory / "quality.json").exists()

    page = result.pages[0]
    assert (
        calculate_file_sha256(page_directory / "processed.png")
        == page.processed_image_sha256
    )
    assert (
        calculate_file_sha256(page_directory / "original_render.png")
        == page.original_render_sha256
    )


def test_preprocess_document_never_overwrites_the_original_render(tmp_path):
    source_path = _write_source_image(tmp_path)
    config = _config(tmp_path)
    preprocessing_input = PreprocessingInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_path=source_path,
        source_sha256=calculate_file_sha256(source_path),
    )

    result = preprocess_document(preprocessing_input, config)
    page_directory = (
        config.artifact_root
        / str(preprocessing_input.batch_id)
        / str(preprocessing_input.document_id)
        / config.preprocessing_version
        / "pages"
        / "page_001"
    )
    original_bytes_before = (page_directory / "original_render.png").read_bytes()

    # Re-running with the same (still-valid) input reproduces the same
    # unmodified render; the original is never mutated in place.
    preprocess_document(preprocessing_input, config)
    original_bytes_after = (page_directory / "original_render.png").read_bytes()

    assert original_bytes_before == original_bytes_after


def test_preprocess_document_flags_review_required_for_a_low_quality_page(tmp_path):
    small_image = np.full((100, 100, 3), 128, dtype=np.uint8)
    source_path = _write_source_image(tmp_path, image=small_image)
    config = _config(tmp_path)
    preprocessing_input = PreprocessingInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_path=source_path,
        source_sha256=calculate_file_sha256(source_path),
    )

    result = preprocess_document(preprocessing_input, config)

    assert result.status == PreprocessingStatus.REVIEW_REQUIRED
    assert result.event.review_required is True
    assert "LOW_RESOLUTION" in result.pages[0].quality.quality_flags


def test_preprocess_document_rerun_is_idempotent(tmp_path):
    source_path = _write_source_image(tmp_path)
    config = _config(tmp_path)
    preprocessing_input = PreprocessingInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_path=source_path,
        source_sha256=calculate_file_sha256(source_path),
    )

    first = preprocess_document(preprocessing_input, config)
    second = preprocess_document(preprocessing_input, config)

    assert first.status == second.status == PreprocessingStatus.SUCCEEDED
    assert first.pages[0].processed_image_sha256 == second.pages[0].processed_image_sha256
    assert first.pages[0].original_render_sha256 == second.pages[0].original_render_sha256


def test_preprocess_document_isolates_artifacts_across_documents(tmp_path):
    config = _config(tmp_path)
    first_source = _write_source_image(tmp_path, name="first.png")
    second_source = _write_source_image(tmp_path, name="second.png")

    first_input = PreprocessingInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_path=first_source,
        source_sha256=calculate_file_sha256(first_source),
    )
    second_input = PreprocessingInput(
        batch_id=first_input.batch_id,
        document_id=uuid4(),
        source_path=second_source,
        source_sha256=calculate_file_sha256(second_source),
    )

    preprocess_document(first_input, config)
    preprocess_document(second_input, config)

    first_directory = (
        config.artifact_root
        / str(first_input.batch_id)
        / str(first_input.document_id)
    )
    second_directory = (
        config.artifact_root
        / str(second_input.batch_id)
        / str(second_input.document_id)
    )
    first_files = {p.name for p in first_directory.rglob("*") if p.is_file()}
    second_files = {p.name for p in second_directory.rglob("*") if p.is_file()}

    assert str(second_input.document_id) not in "".join(
        str(p) for p in first_directory.rglob("*")
    )
    assert first_files and second_files

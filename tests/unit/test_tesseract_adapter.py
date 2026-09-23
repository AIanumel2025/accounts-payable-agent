"""Unit tests for `ap_agent.adapters.tesseract_adapter` (notebook cells 36,
43). Uses the real local Tesseract binary (no network dependency; system
`tesseract-ocr`/`tesseract-ocr-eng` must be installed) against small,
generated images.
"""

from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from ap_agent.adapters.tesseract_adapter import (
    build_evidence_lines,
    build_ocr_tokens,
    build_raw_ocr_rows,
    extract_tesseract_fallback_page,
    get_tesseract_version,
    run_tesseract_page,
)
from ap_agent.artifacts.filesystem import calculate_file_sha256
from ap_agent.config.settings import OCRConfig
from ap_agent.models.ocr import OCRPageInput, OCRStatus

pytestmark = [pytest.mark.unit, pytest.mark.ocr]


def _text_image_path(tmp_path, text="INVOICE TOTAL 123.45", name="page.png"):
    import cv2

    image = np.full((300, 900, 3), 255, dtype=np.uint8)
    cv2.putText(
        image, text, (20, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 0), 2
    )
    path = tmp_path / name
    cv2.imwrite(str(path), image)
    return path


def _ocr_config(tmp_path, **overrides):
    return OCRConfig(artifact_root=tmp_path / "ocr", **overrides)


# --- version probe -----------------------------------------------------------


def test_get_tesseract_version_returns_a_nonempty_string():
    version = get_tesseract_version()
    assert isinstance(version, str)
    assert version


# --- raw extraction ------------------------------------------------------------


def test_run_tesseract_page_returns_expected_keys(tmp_path):
    image_path = _text_image_path(tmp_path)
    config = _ocr_config(tmp_path)

    ocr_data = run_tesseract_page(image_path, config)

    for key in ("text", "conf", "left", "top", "width", "height", "block_num", "par_num", "line_num", "word_num"):
        assert key in ocr_data


def test_run_tesseract_page_raises_on_missing_image(tmp_path):
    config = _ocr_config(tmp_path)
    with pytest.raises(FileNotFoundError):
        run_tesseract_page(tmp_path / "missing.png", config)


def test_run_tesseract_page_recognizes_expected_text(tmp_path):
    image_path = _text_image_path(tmp_path, text="HELLO WORLD")
    config = _ocr_config(tmp_path)

    ocr_data = run_tesseract_page(image_path, config)
    recognized = " ".join(t for t in ocr_data["text"] if t.strip())
    assert "HELLO" in recognized.upper() or "WORLD" in recognized.upper()


# --- token/line construction ---------------------------------------------------


def test_build_ocr_tokens_skips_structural_rows_with_negative_confidence(tmp_path):
    ocr_data = {
        "text": ["", "WORD"],
        "conf": ["-1", "95.5"],
        "left": [0, 10],
        "top": [0, 10],
        "width": [0, 50],
        "height": [0, 20],
        "block_num": [0, 1],
        "par_num": [0, 1],
        "line_num": [0, 1],
        "word_num": [0, 1],
    }
    page_input = OCRPageInput(
        page_number=1,
        processed_image_path=Path("page.png"),
        processed_image_sha256="a" * 64,
    )
    config = _ocr_config(tmp_path)

    tokens = build_ocr_tokens(
        ocr_data, document_id=uuid4(), page_input=page_input,
        image_width=1000, image_height=1000, config=config,
    )

    assert len(tokens) == 1
    assert tokens[0].text == "WORD"
    assert tokens[0].confidence == 95.5


def test_build_ocr_tokens_assigns_sequential_reading_order(tmp_path):
    ocr_data = {
        "text": ["FIRST", "SECOND", "THIRD"],
        "conf": ["90", "91", "92"],
        "left": [0, 100, 200],
        "top": [0, 0, 0],
        "width": [50, 50, 50],
        "height": [20, 20, 20],
        "block_num": [1, 1, 1],
        "par_num": [1, 1, 1],
        "line_num": [1, 1, 1],
        "word_num": [1, 2, 3],
    }
    page_input = OCRPageInput(
        page_number=1,
        processed_image_path=Path("page.png"),
        processed_image_sha256="a" * 64,
    )
    config = _ocr_config(tmp_path)

    tokens = build_ocr_tokens(
        ocr_data, document_id=uuid4(), page_input=page_input,
        image_width=1000, image_height=1000, config=config,
    )
    assert [t.reading_order for t in tokens] == [1, 2, 3]


def test_build_evidence_lines_groups_tokens_by_block_paragraph_line(tmp_path):
    ocr_data = {
        "text": ["ROW1A", "ROW1B", "ROW2A"],
        "conf": ["90", "91", "92"],
        "left": [0, 60, 0],
        "top": [0, 0, 40],
        "width": [50, 50, 50],
        "height": [20, 20, 20],
        "block_num": [1, 1, 1],
        "par_num": [1, 1, 1],
        "line_num": [1, 1, 2],
        "word_num": [1, 2, 1],
    }
    page_input = OCRPageInput(
        page_number=1,
        processed_image_path=Path("page.png"),
        processed_image_sha256="a" * 64,
    )
    config = _ocr_config(tmp_path)
    tokens = build_ocr_tokens(
        ocr_data, document_id=uuid4(), page_input=page_input,
        image_width=1000, image_height=1000, config=config,
    )

    lines = build_evidence_lines(tokens, document_id=uuid4(), page_input=page_input, config=config)

    assert len(lines) == 2
    assert lines[0].text == "ROW1A ROW1B"
    assert lines[1].text == "ROW2A"
    assert len(lines[0].token_evidence_ids) == 2


def test_build_raw_ocr_rows_round_trips_every_column(tmp_path):
    ocr_data = {"text": ["A", "B"], "conf": ["90", "91"]}
    rows = build_raw_ocr_rows(ocr_data)
    assert rows == [{"text": "A", "conf": "90"}, {"text": "B", "conf": "91"}]


# --- extract_tesseract_fallback_page (final active adapter) -------------------


def test_extract_tesseract_fallback_page_verifies_the_source_hash(tmp_path):
    image_path = _text_image_path(tmp_path)
    page_input = OCRPageInput(
        page_number=1,
        processed_image_path=image_path,
        processed_image_sha256="0" * 64,  # deliberately wrong
    )
    config = _ocr_config(tmp_path)

    with pytest.raises(ValueError, match="SHA-256"):
        extract_tesseract_fallback_page(uuid4(), page_input, config)


def test_extract_tesseract_fallback_page_produces_a_succeeded_result(tmp_path):
    image_path = _text_image_path(tmp_path, text="INVOICE NUMBER 12345 TOTAL 99.00")
    page_input = OCRPageInput(
        page_number=1,
        processed_image_path=image_path,
        processed_image_sha256=calculate_file_sha256(image_path),
    )
    config = _ocr_config(tmp_path)

    page_result, raw_rows = extract_tesseract_fallback_page(uuid4(), page_input, config)

    assert page_result.ocr_engine == "tesseract-fallback"
    assert page_result.ocr_engine_version == get_tesseract_version()
    assert page_result.evidence_image_path == image_path
    assert page_result.evidence_image_sha256 == page_result.processed_image_sha256
    assert isinstance(raw_rows, list)
    assert len(page_result.tokens) > 0


def test_extract_tesseract_fallback_page_flags_insufficient_tokens_on_a_blank_page(tmp_path):
    import cv2

    blank = np.full((300, 900, 3), 255, dtype=np.uint8)
    image_path = tmp_path / "blank.png"
    cv2.imwrite(str(image_path), blank)

    page_input = OCRPageInput(
        page_number=1,
        processed_image_path=image_path,
        processed_image_sha256=calculate_file_sha256(image_path),
    )
    config = _ocr_config(tmp_path)

    page_result, _ = extract_tesseract_fallback_page(uuid4(), page_input, config)

    assert page_result.status == OCRStatus.REVIEW_REQUIRED
    assert "INSUFFICIENT_OCR_TOKENS" in page_result.review_reasons


def test_extract_tesseract_fallback_page_propagates_preprocessing_review_flag(tmp_path):
    image_path = _text_image_path(tmp_path, text="INVOICE NUMBER 12345 TOTAL 99.00")
    page_input = OCRPageInput(
        page_number=1,
        processed_image_path=image_path,
        processed_image_sha256=calculate_file_sha256(image_path),
        preprocessing_review_required=True,
    )
    config = _ocr_config(tmp_path)

    page_result, _ = extract_tesseract_fallback_page(uuid4(), page_input, config)

    assert "PREPROCESSING_REVIEW_REQUIRED" in page_result.review_reasons

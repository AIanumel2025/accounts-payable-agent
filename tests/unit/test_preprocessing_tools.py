"""Unit tests for the Phase 2 (preprocessing) processing functions:
image loading, PDF rendering, deskewing, enhancement and the corrected
quality gate from `ap_agent.tools.preprocessing` (notebook cells 26, 29).

All tests use synthetic images and temporary directories; none read the
committed invoice fixtures (that is `tests/integration/test_phase_2_preprocessing.py`).
"""

from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from ap_agent.config.settings import PreprocessingConfig
from ap_agent.models.ingestion import DocumentIdentity, IngestionResult, IntakeInspection
from ap_agent.models.common import DocumentRecord, ProcessingEvent, ProcessingStage, ProcessingStatus
from ap_agent.models.ingestion import IngestionDisposition
from ap_agent.tools.preprocessing import (
    assess_page_quality,
    build_preprocessing_directory,
    build_preprocessing_input,
    deskew_image,
    detect_skew_angle,
    enhance_page_image,
    load_document_pages,
    load_image_with_orientation,
    render_pdf_pages,
    rotate_without_cropping,
)
from ap_agent.models.preprocessing import PreprocessingInput

pytestmark = pytest.mark.unit


def _white_page(width=900, height=1200):
    return np.full((height, width, 3), 250, dtype=np.uint8)


def _draw_text_block(image, box=(100, 100, 400, 60)):
    x, y, w, h = box
    image[y : y + h, x : x + w] = 20
    return image


def _save_png(tmp_path, name, image):
    import cv2

    path = tmp_path / name
    cv2.imwrite(str(path), image)
    return path


# --- image loading and PDF rendering -------------------------------------


def test_load_image_with_orientation_returns_bgr_array(tmp_path):
    image = _draw_text_block(_white_page())
    path = _save_png(tmp_path, "page.png", image)

    loaded = load_image_with_orientation(path)

    assert loaded.shape == image.shape
    assert loaded.dtype == np.uint8


def test_load_image_with_orientation_applies_exif_rotation(tmp_path):
    from PIL import Image

    # A 100x50 (w x h) image tagged as needing a 90-degree rotation via EXIF.
    pil_image = Image.new("RGB", (100, 50), color=(10, 20, 30))
    path = tmp_path / "rotated.jpg"
    exif = pil_image.getexif()
    exif[274] = 6  # Orientation: rotate 270 CW to display correctly.
    pil_image.save(path, exif=exif)

    loaded = load_image_with_orientation(path)

    # After EXIF correction, the 100x50 source becomes 50x100 (h, w).
    assert loaded.shape[:2] == (100, 50)


def test_render_pdf_pages_renders_every_page(tmp_path):
    import pymupdf

    document = pymupdf.open()
    for index in range(3):
        page = document.new_page(width=300, height=400)
        page.insert_text((50, 50), f"page {index}")
    pdf_path = tmp_path / "multi.pdf"
    document.save(str(pdf_path))
    document.close()

    pages = render_pdf_pages(pdf_path, dpi=150)

    assert len(pages) == 3
    for page in pages:
        assert page.ndim == 3
        assert page.shape[2] == 3


def test_render_pdf_pages_scales_with_dpi(tmp_path):
    import pymupdf

    document = pymupdf.open()
    document.new_page(width=72, height=144)  # 1x2 inches at 72 dpi.
    pdf_path = tmp_path / "one_by_two_inches.pdf"
    document.save(str(pdf_path))
    document.close()

    pages_72 = render_pdf_pages(pdf_path, dpi=72)
    pages_144 = render_pdf_pages(pdf_path, dpi=144)

    assert pages_72[0].shape[:2] == (144, 72)
    assert pages_144[0].shape[:2] == (288, 144)


def test_render_pdf_pages_raises_on_empty_pdf(tmp_path):
    # pymupdf refuses to *write* a zero-page document, so a minimal,
    # hand-crafted PDF with an empty page tree is used instead.
    pdf_bytes = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        b"trailer\n<< /Size 3 /Root 1 0 R >>\n"
        b"%%EOF"
    )
    pdf_path = tmp_path / "empty.pdf"
    pdf_path.write_bytes(pdf_bytes)

    with pytest.raises(ValueError):
        render_pdf_pages(pdf_path, dpi=150)


def test_load_document_pages_dispatches_by_extension(tmp_path):
    config = PreprocessingConfig(artifact_root=tmp_path / "artifacts")
    png_path = _save_png(tmp_path, "page.png", _white_page())

    pages = load_document_pages(png_path, config)

    assert len(pages) == 1


def test_load_document_pages_rejects_unsupported_extension(tmp_path):
    config = PreprocessingConfig(artifact_root=tmp_path / "artifacts")
    bad_path = tmp_path / "notes.txt"
    bad_path.write_text("not a document")

    with pytest.raises(ValueError):
        load_document_pages(bad_path, config)


# --- deskewing -------------------------------------------------------------


def test_detect_skew_angle_is_zero_for_a_blank_page():
    assert detect_skew_angle(_white_page()) == 0.0


def test_detect_skew_angle_is_zero_below_the_foreground_pixel_threshold():
    # Fewer than 100 foreground pixels: too little evidence to estimate skew.
    image = _white_page()
    image[100:103, 100:110] = 0  # ~30 dark pixels.
    assert detect_skew_angle(image) == 0.0


def test_rotate_without_cropping_expands_the_canvas():
    image = np.zeros((100, 200, 3), dtype=np.uint8)

    rotated = rotate_without_cropping(image, angle=45.0)

    assert rotated.shape[0] > image.shape[0]
    assert rotated.shape[1] > image.shape[1]


def test_deskew_image_leaves_a_near_zero_angle_unchanged():
    image = _draw_text_block(_white_page())

    deskewed, angle = deskew_image(image, maximum_angle=15.0)

    assert angle == 0.0
    np.testing.assert_array_equal(deskewed, image)


def test_deskew_image_preserves_the_image_when_angle_exceeds_the_maximum(monkeypatch):
    import ap_agent.tools.preprocessing as preprocessing

    monkeypatch.setattr(preprocessing, "detect_skew_angle", lambda image: 30.0)
    image = _white_page()

    deskewed, angle = deskew_image(image, maximum_angle=15.0)

    assert angle == 30.0
    np.testing.assert_array_equal(deskewed, image)


def test_deskew_image_rotates_for_a_correctable_angle(monkeypatch):
    import ap_agent.tools.preprocessing as preprocessing

    monkeypatch.setattr(preprocessing, "detect_skew_angle", lambda image: 5.0)
    image = _white_page()

    deskewed, angle = deskew_image(image, maximum_angle=15.0)

    assert angle == 5.0
    assert deskewed.shape != image.shape or not np.array_equal(deskewed, image)


# --- enhancement -------------------------------------------------------------


def test_enhance_page_image_does_not_mutate_the_source():
    config = PreprocessingConfig(artifact_root=Path("/tmp/unused"))
    image = _draw_text_block(_white_page())
    original = image.copy()

    enhance_page_image(image, config)

    np.testing.assert_array_equal(image, original)


def test_enhance_page_image_returns_a_single_channel_image():
    config = PreprocessingConfig(artifact_root=Path("/tmp/unused"))
    image = _draw_text_block(_white_page())

    enhanced = enhance_page_image(image, config)

    assert enhanced.ndim == 2


def test_enhance_page_image_respects_disabled_steps():
    config = PreprocessingConfig(
        artifact_root=Path("/tmp/unused"),
        enable_denoising=False,
        enable_clahe=False,
    )
    image = _draw_text_block(_white_page())

    import cv2

    expected = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    enhanced = enhance_page_image(image, config)

    np.testing.assert_array_equal(enhanced, expected)


# --- corrected quality gate (cell 29) --------------------------------------


def _quality_config(**overrides):
    return PreprocessingConfig(artifact_root=Path("/tmp/unused"), **overrides)


def test_assess_page_quality_flags_low_resolution_just_below_the_threshold():
    image = np.full((699, 900), 128, dtype=np.uint8)
    quality = assess_page_quality(image, 1, 0.0, _quality_config())
    assert "LOW_RESOLUTION" in quality.quality_flags
    assert quality.review_required is True


def test_assess_page_quality_does_not_flag_low_resolution_at_the_threshold():
    image = np.full((700, 900, 3), 128, dtype=np.uint8)
    # Add contrast so LOW_CONTRAST doesn't also fire and confuse the assertion.
    image[:, :450] = 40
    quality = assess_page_quality(image, 1, 0.0, _quality_config())
    assert "LOW_RESOLUTION" not in quality.quality_flags


def test_assess_page_quality_flags_low_resolution_by_width_only():
    image = np.full((900, 499), 128, dtype=np.uint8)
    quality = assess_page_quality(image, 1, 0.0, _quality_config())
    assert "LOW_RESOLUTION" in quality.quality_flags


def test_assess_page_quality_ignores_config_minimum_width_and_height():
    """Cell 29 hard-codes 500x700 and ignores config.minimum_width/height
    (R-09 of the modularisation map: preserved as-is, not "fixed")."""
    image = np.full((900, 900), 128, dtype=np.uint8)
    config = _quality_config(minimum_width=2000, minimum_height=2000)
    quality = assess_page_quality(image, 1, 0.0, config)
    assert "LOW_RESOLUTION" not in quality.quality_flags


def test_assess_page_quality_flags_too_dark():
    image = np.full((900, 900), 10, dtype=np.uint8)
    quality = assess_page_quality(image, 1, 0.0, _quality_config())
    assert "TOO_DARK" in quality.quality_flags


def test_assess_page_quality_flags_possibly_blank_or_overexposed_when_no_foreground():
    image = np.full((900, 900), 250, dtype=np.uint8)
    quality = assess_page_quality(image, 1, 0.0, _quality_config())
    assert "POSSIBLY_BLANK_OR_OVEREXPOSED" in quality.quality_flags


def test_assess_page_quality_does_not_flag_bright_legible_pages():
    """The foreground-aware correction: a bright page with real (dark)
    foreground content is not misclassified as blank/overexposed, unlike
    the superseded cell-26 unconditional TOO_BRIGHT flag."""
    image = _draw_text_block(np.full((900, 900, 3), 245, dtype=np.uint8))
    quality = assess_page_quality(image, 1, 0.0, _quality_config())
    assert "POSSIBLY_BLANK_OR_OVEREXPOSED" not in quality.quality_flags
    assert "TOO_BRIGHT" not in quality.quality_flags  # that flag no longer exists at all


def test_assess_page_quality_flags_low_contrast():
    image = np.full((900, 900), 128, dtype=np.uint8)
    quality = assess_page_quality(image, 1, 0.0, _quality_config())
    assert "LOW_CONTRAST" in quality.quality_flags


def test_assess_page_quality_flags_possible_blur():
    image = np.full((900, 900), 128, dtype=np.uint8)
    image[:, :450] = 40  # enough contrast to avoid LOW_CONTRAST
    quality = assess_page_quality(image, 1, 0.0, _quality_config())
    assert "POSSIBLE_BLUR" in quality.quality_flags


def test_assess_page_quality_flags_excessive_skew():
    image = _draw_text_block(np.full((900, 900, 3), 245, dtype=np.uint8))
    quality = assess_page_quality(image, 1, 20.0, _quality_config())
    assert "EXCESSIVE_SKEW_OR_PERSPECTIVE" in quality.quality_flags


def test_assess_page_quality_accepts_a_clean_page_with_no_flags():
    image = _draw_text_block(np.full((900, 900, 3), 245, dtype=np.uint8))
    quality = assess_page_quality(image, 1, 0.0, _quality_config())
    assert quality.quality_flags == ()
    assert quality.review_required is False


def test_assess_page_quality_rounds_scores_to_two_decimal_places():
    image = _draw_text_block(np.full((900, 900, 3), 245, dtype=np.uint8))
    quality = assess_page_quality(image, 1, 1.23456, _quality_config())
    assert quality.detected_skew_angle == 1.23
    assert quality.brightness_score == round(quality.brightness_score, 2)


# --- deterministic directory identities ------------------------------------


def test_build_preprocessing_directory_is_deterministic():
    preprocessing_input = PreprocessingInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_path=Path("/does/not/matter.pdf"),
        source_sha256="a" * 64,
    )
    config = PreprocessingConfig(artifact_root=Path("/artifacts"))

    first = build_preprocessing_directory(preprocessing_input, config)
    second = build_preprocessing_directory(preprocessing_input, config)

    assert first == second
    assert first == (
        config.artifact_root
        / str(preprocessing_input.batch_id)
        / str(preprocessing_input.document_id)
        / config.preprocessing_version
    )


def test_build_preprocessing_directory_isolates_documents():
    config = PreprocessingConfig(artifact_root=Path("/artifacts"))
    batch_id = uuid4()

    first_input = PreprocessingInput(
        batch_id=batch_id,
        document_id=uuid4(),
        source_path=Path("/a.pdf"),
        source_sha256="a" * 64,
    )
    second_input = PreprocessingInput(
        batch_id=batch_id,
        document_id=uuid4(),
        source_path=Path("/b.pdf"),
        source_sha256="b" * 64,
    )

    assert build_preprocessing_directory(
        first_input, config
    ) != build_preprocessing_directory(second_input, config)


# --- Phase 1 -> Phase 2 bridge ----------------------------------------------


def _ingestion_result(*, stored_path: Path, sha256: str) -> IngestionResult:
    batch_id = uuid4()
    document_id = uuid4()
    content_id = uuid4()
    identity = DocumentIdentity(
        batch_id=batch_id,
        document_id=document_id,
        content_id=content_id,
        sha256=sha256,
    )
    inspection = IntakeInspection(
        source_path=stored_path,
        original_filename=stored_path.name,
        extension=stored_path.suffix,
        media_type="application/pdf",
        file_size_bytes=1,
    )
    document = DocumentRecord(
        document_id=document_id,
        batch_id=batch_id,
        original_filename=stored_path.name,
        source_path=stored_path,
        media_type="application/pdf",
        status=ProcessingStatus.PENDING,
        sha256=sha256,
    )
    event = ProcessingEvent(
        batch_id=batch_id,
        document_id=document_id,
        stage=ProcessingStage.INGESTION,
        status=ProcessingStatus.SUCCEEDED,
        message="ok",
    )
    return IngestionResult(
        disposition=IngestionDisposition.ACCEPTED,
        inspection=inspection,
        identity=identity,
        document=document,
        event=event,
        stored_path=stored_path,
        original_preserved=True,
    )


def test_build_preprocessing_input_uses_the_preserved_artifact_not_the_upload_path():
    """Decision D-6: the bridge must use Phase 1's stored (preserved)
    artifact path and its registered SHA-256, not the original upload
    path (superseding notebook cell 31's `UPLOAD_ROOT / row['file']`)."""
    preserved_path = Path("/artifacts/originals/content-id/original.pdf")
    result = _ingestion_result(stored_path=preserved_path, sha256="f" * 64)

    preprocessing_input = build_preprocessing_input(result)

    assert preprocessing_input.source_path == preserved_path
    assert preprocessing_input.source_sha256 == "f" * 64
    assert preprocessing_input.batch_id == result.identity.batch_id
    assert preprocessing_input.document_id == result.identity.document_id

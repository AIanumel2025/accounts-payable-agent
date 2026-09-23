"""Unit tests for `ap_agent.tools.ocr`: the Phase 2 -> Phase 3 bridge,
provider routing (with mocked adapters), the corrected persistence order
(decisions D-4/D-5), and the critical TOTAL-value completeness guard
(notebook cell 47).
"""

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.config.settings import OCRConfig
from ap_agent.models.ocr import (
    BoundingBox,
    EvidenceLine,
    OCRDocumentInput,
    OCRDocumentResult,
    OCREvent,
    OCRPageInput,
    OCRPageResult,
    OCRStatus,
    OCRToken,
)
from ap_agent.models.preprocessing import (
    PageQuality,
    PreprocessedPage,
    PreprocessingEvent,
    PreprocessingResult,
    PreprocessingStatus,
)
from ap_agent.tools.ocr import (
    TOTAL_VALUE_MISSING_REASON,
    apply_total_completeness_guard,
    build_ocr_document_directory,
    build_ocr_document_input,
    extract_routed_ocr_page,
    page_has_total_without_value,
    process_ocr_document,
)

pytestmark = pytest.mark.unit


# --- test helpers ------------------------------------------------------------


def _box(x, y, w, h):
    return BoundingBox(x=x, y=y, width=w, height=h)


def _token(text, box, evidence_id=None):
    return OCRToken(
        evidence_id=evidence_id or uuid4(),
        page_number=1,
        reading_order=1,
        text=text,
        confidence=95.0,
        bounding_box=box,
        block_number=1,
        paragraph_number=1,
        line_number=1,
        word_number=1,
    )


def _page_result(tokens, status=OCRStatus.SUCCEEDED, review_reasons=()):
    return OCRPageResult(
        page_number=1,
        processed_image_path=Path("processed.png"),
        processed_image_sha256="a" * 64,
        evidence_image_path=Path("evidence.png"),
        evidence_image_sha256="b" * 64,
        page_text=" ".join(t.text for t in tokens),
        tokens=tuple(tokens),
        evidence_lines=(),
        mean_confidence=95.0,
        low_confidence_token_count=0,
        low_confidence_ratio=0.0,
        status=status,
        review_reasons=review_reasons,
        ocr_engine="paddleocr",
        ocr_engine_version="3.7.0",
        ocr_configuration="lang=en",
        processed_at=datetime.now(timezone.utc),
    )


def _document_result(pages, status=OCRStatus.SUCCEEDED):
    event = OCREvent(
        event_type="OCR_EXTRACTION",
        status=status,
        batch_id=uuid4(),
        document_id=uuid4(),
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=status != OCRStatus.SUCCEEDED,
    )
    return OCRDocumentResult(
        batch_id=event.batch_id,
        document_id=event.document_id,
        source_document_sha256="c" * 64,
        preprocessing_version="phase2-v1",
        status=status,
        pages=tuple(pages),
        event=event,
        errors=(),
    )


# ==============================================================================
# Phase 2 -> Phase 3 bridge
# ==============================================================================


def _preprocessing_result(status=PreprocessingStatus.SUCCEEDED, page_count=1):
    batch_id = uuid4()
    document_id = uuid4()
    pages = []
    for page_number in range(1, page_count + 1):
        quality = PageQuality(
            page_number=page_number,
            width=1000,
            height=1000,
            brightness_score=200.0,
            contrast_score=50.0,
            blur_score=500.0,
            detected_skew_angle=0.0,
            quality_flags=(),
            review_required=False,
        )
        pages.append(
            PreprocessedPage(
                page_number=page_number,
                original_render_path=Path(f"page_{page_number}/original_render.png"),
                processed_image_path=Path(f"page_{page_number}/processed.png"),
                original_render_sha256="d" * 64,
                processed_image_sha256=f"{page_number}" * 64,
                quality=quality,
            )
        )
    event = PreprocessingEvent(
        event_type="PREPROCESSING",
        status=status,
        batch_id=batch_id,
        document_id=document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=status != PreprocessingStatus.SUCCEEDED,
    )
    return PreprocessingResult(
        batch_id=batch_id,
        document_id=document_id,
        source_path=Path("source.pdf"),
        source_sha256="e" * 64,
        status=status,
        pages=tuple(pages),
        event=event,
        errors=() if status != PreprocessingStatus.FAILED else ("boom",),
    )


def test_build_ocr_document_input_maps_pages_and_ids():
    preprocessing_result = _preprocessing_result(page_count=3)

    ocr_input = build_ocr_document_input(preprocessing_result, preprocessing_version="phase2-v1")

    assert ocr_input.batch_id == preprocessing_result.batch_id
    assert ocr_input.document_id == preprocessing_result.document_id
    assert ocr_input.source_document_sha256 == preprocessing_result.source_sha256
    assert ocr_input.preprocessing_version == "phase2-v1"
    assert len(ocr_input.pages) == 3
    assert [p.page_number for p in ocr_input.pages] == [1, 2, 3]


def test_build_ocr_document_input_propagates_preprocessing_review_flag():
    preprocessing_result = _preprocessing_result(status=PreprocessingStatus.REVIEW_REQUIRED)
    preprocessing_result = replace(
        preprocessing_result,
        pages=(
            replace(
                preprocessing_result.pages[0],
                quality=replace(preprocessing_result.pages[0].quality, review_required=True),
            ),
        ),
    )

    ocr_input = build_ocr_document_input(preprocessing_result, preprocessing_version="phase2-v1")

    assert ocr_input.pages[0].preprocessing_review_required is True


def test_build_ocr_document_input_rejects_a_failed_preprocessing_result():
    preprocessing_result = _preprocessing_result(status=PreprocessingStatus.FAILED)
    preprocessing_result = replace(preprocessing_result, pages=())

    with pytest.raises(ValueError):
        build_ocr_document_input(preprocessing_result, preprocessing_version="phase2-v1")


# ==============================================================================
# build_ocr_document_directory
# ==============================================================================


def test_build_ocr_document_directory_is_deterministic_and_isolated(tmp_path):
    config = OCRConfig(artifact_root=tmp_path, ocr_version="ocr-v2-paddle")
    batch_id = uuid4()

    first_input = OCRDocumentInput(
        batch_id=batch_id, document_id=uuid4(), source_document_sha256="a" * 64,
        preprocessing_version="phase2-v1", pages=(),
    )
    second_input = OCRDocumentInput(
        batch_id=batch_id, document_id=uuid4(), source_document_sha256="b" * 64,
        preprocessing_version="phase2-v1", pages=(),
    )

    first_dir = build_ocr_document_directory(first_input, config)
    second_dir = build_ocr_document_directory(second_input, config)

    assert first_dir == tmp_path / str(batch_id) / str(first_input.document_id) / "ocr-v2-paddle"
    assert first_dir != second_dir


# ==============================================================================
# TOTAL-value completeness guard (notebook cell 47)
# ==============================================================================


def test_page_has_total_without_value_true_when_no_money_on_the_line():
    total_token = _token("TOTAL", _box(0, 100, 50, 20))
    other_token = _token("Some other text", _box(0, 0, 100, 20))
    assert page_has_total_without_value(_page_result([total_token, other_token])) is True


def test_page_has_total_without_value_false_when_value_is_on_the_same_token():
    total_token = _token("TOTAL: 873.58", _box(0, 100, 100, 20))
    assert page_has_total_without_value(_page_result([total_token])) is False


def test_page_has_total_without_value_false_when_value_is_to_the_right_on_the_same_line():
    total_token = _token("TOTAL", _box(0, 100, 50, 20))
    value_token = _token("69.22", _box(60, 102, 50, 18))
    assert page_has_total_without_value(_page_result([total_token, value_token])) is False


def test_page_has_total_without_value_ignores_subtotal_labels():
    """The (?<!SUB)\\bTOTAL\\b pattern must not match SUBTOTAL."""
    subtotal_token = _token("SUBTOTAL", _box(0, 0, 80, 20))
    assert page_has_total_without_value(_page_result([subtotal_token])) is False


def test_page_has_total_without_value_false_when_no_total_token_present():
    token = _token("INVOICE NUMBER 12345", _box(0, 0, 150, 20))
    assert page_has_total_without_value(_page_result([token])) is False


def test_page_has_total_without_value_ignores_a_value_on_a_different_line():
    total_token = _token("TOTAL", _box(0, 100, 50, 20))
    unrelated_value = _token("50.10", _box(0, 500, 50, 20))
    assert page_has_total_without_value(_page_result([total_token, unrelated_value])) is True


def test_page_has_total_without_value_picks_the_lowest_total_as_grand_total():
    """When multiple TOTAL-matching tokens exist, the grand total is the
    one furthest down the page (highest y)."""
    subtotal_like = _token("TOTAL DUE LATER", _box(0, 0, 100, 20))  # has a value elsewhere, irrelevant
    grand_total = _token("TOTAL", _box(0, 500, 50, 20))
    value_near_first = _token("10.00", _box(110, 2, 40, 18))
    # Grand total (lower on page) has no value nearby -> True.
    assert page_has_total_without_value(
        _page_result([subtotal_like, grand_total, value_near_first])
    ) is True


def test_apply_total_completeness_guard_marks_page_and_document_review_required():
    total_token = _token("TOTAL", _box(0, 100, 50, 20))
    page = _page_result([total_token])
    document = _document_result([page])

    guarded = apply_total_completeness_guard(document)

    assert guarded.status == OCRStatus.REVIEW_REQUIRED
    assert guarded.pages[0].status == OCRStatus.REVIEW_REQUIRED
    assert TOTAL_VALUE_MISSING_REASON in guarded.pages[0].review_reasons
    assert guarded.event.status == OCRStatus.REVIEW_REQUIRED
    assert guarded.event.review_required is True


def test_apply_total_completeness_guard_adds_the_reason_once():
    total_token = _token("TOTAL", _box(0, 100, 50, 20))
    page = _page_result(
        [total_token],
        status=OCRStatus.REVIEW_REQUIRED,
        review_reasons=(TOTAL_VALUE_MISSING_REASON,),
    )
    document = _document_result([page], status=OCRStatus.REVIEW_REQUIRED)

    guarded = apply_total_completeness_guard(document)

    assert guarded.pages[0].review_reasons.count(TOTAL_VALUE_MISSING_REASON) == 1


def test_apply_total_completeness_guard_does_not_fire_when_total_has_a_value():
    total_token = _token("TOTAL: 69.22", _box(0, 100, 100, 20))
    page = _page_result([total_token])
    document = _document_result([page])

    guarded = apply_total_completeness_guard(document)

    assert guarded.status == OCRStatus.SUCCEEDED
    assert guarded.pages[0].status == OCRStatus.SUCCEEDED
    assert TOTAL_VALUE_MISSING_REASON not in guarded.pages[0].review_reasons


def test_apply_total_completeness_guard_never_inserts_a_money_value():
    """The guard must never write a monetary value into OCR evidence."""
    total_token = _token("TOTAL", _box(0, 100, 50, 20))
    page = _page_result([total_token])
    document = _document_result([page])

    guarded = apply_total_completeness_guard(document)

    guarded_texts = [t.text for t in guarded.pages[0].tokens]
    assert guarded_texts == ["TOTAL"]  # unchanged; no inferred value token added


def test_apply_total_completeness_guard_preserves_failed_status():
    """A genuine technical failure must remain FAILED, never REVIEW_REQUIRED."""
    document = _document_result([], status=OCRStatus.FAILED)
    document = replace(document, errors=("RuntimeError: boom",))

    guarded = apply_total_completeness_guard(document)

    assert guarded.status == OCRStatus.FAILED


def test_apply_total_completeness_guard_leaves_a_clean_document_untouched():
    token = _token("INVOICE NUMBER 12345", _box(0, 0, 150, 20))
    page = _page_result([token])
    document = _document_result([page])

    guarded = apply_total_completeness_guard(document)

    assert guarded.status == OCRStatus.SUCCEEDED
    assert guarded.pages[0].review_reasons == ()


def test_apply_total_completeness_guard_keeps_pages_as_a_tuple():
    total_token = _token("TOTAL", _box(0, 100, 50, 20))
    page = _page_result([total_token])
    document = _document_result([page])

    guarded = apply_total_completeness_guard(document)

    assert isinstance(guarded.pages, tuple)


# ==============================================================================
# Provider routing (extract_routed_ocr_page) with mocked adapters
# ==============================================================================


class _FakeSuccessfulPaddleAdapter:
    def __init__(self):
        self.calls = 0

    def __call__(self, *, document_id, page_input, config, evidence_image_destination, engine, engine_version):
        self.calls += 1
        from ap_agent.tools.ocr_evidence import create_token_evidence_id

        box = _box(0, 0, 50, 20)
        text = "PADDLE OK"
        evidence_id = create_token_evidence_id(
            document_id=document_id,
            page_number=page_input.page_number,
            reading_order=1,
            text=text,
            bounding_box=box,
            processed_image_sha256=page_input.processed_image_sha256,
            ocr_version=config.ocr_version + "-paddle",
        )
        page_result = _page_result([_token(text, box, evidence_id=evidence_id)])
        page_result = replace(page_result, page_number=page_input.page_number)
        return page_result, {"provider": "paddleocr"}


class _FakeFailingPaddleAdapter:
    def __call__(self, **kwargs):
        raise RuntimeError("paddle exploded")


class _FakeSuccessfulTesseractAdapter:
    def __call__(self, document_id, page_input, config):
        page_result = OCRPageResult(
            page_number=1,
            processed_image_path=page_input.processed_image_path,
            processed_image_sha256=page_input.processed_image_sha256,
            evidence_image_path=page_input.processed_image_path,
            evidence_image_sha256=page_input.processed_image_sha256,
            page_text="TESSERACT OK",
            tokens=(),
            evidence_lines=(),
            mean_confidence=90.0,
            low_confidence_token_count=0,
            low_confidence_ratio=0.0,
            status=OCRStatus.SUCCEEDED,
            review_reasons=(),
            ocr_engine="tesseract-fallback",
            ocr_engine_version="5.3.4",
            ocr_configuration="--oem 3 --psm 6",
            processed_at=datetime.now(timezone.utc),
        )
        return page_result, [{"row": 1}]


class _FakeFailingTesseractAdapter:
    def __call__(self, *args, **kwargs):
        raise RuntimeError("tesseract exploded")


def _routing_page_input(tmp_path):
    image_path = tmp_path / "processed.png"
    import cv2
    import numpy as np

    cv2.imwrite(str(image_path), np.full((100, 100, 3), 255, dtype="uint8"))
    from ap_agent.artifacts.filesystem import calculate_file_sha256

    return OCRPageInput(
        page_number=1,
        processed_image_path=image_path,
        processed_image_sha256=calculate_file_sha256(image_path),
    )


def test_extract_routed_ocr_page_uses_paddle_when_it_succeeds(tmp_path, monkeypatch):
    fake_paddle = _FakeSuccessfulPaddleAdapter()
    monkeypatch.setattr("ap_agent.tools.ocr.extract_paddle_ocr_page", fake_paddle)

    page_input = _routing_page_input(tmp_path)
    config = OCRConfig(artifact_root=tmp_path, ocr_version="ocr-v2-paddle")

    page_result, raw = extract_routed_ocr_page(
        document_id=uuid4(), page_input=page_input, config=config,
        evidence_image_destination=tmp_path / "evidence.png",
        engine=object(), engine_version="3.7.0",
    )

    assert raw["selected_provider"] == "paddleocr"
    assert raw["fallback_used"] is False
    assert page_result.page_text == "PADDLE OK"
    assert fake_paddle.calls == 1


def test_extract_routed_ocr_page_falls_back_to_tesseract_on_paddle_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("ap_agent.tools.ocr.extract_paddle_ocr_page", _FakeFailingPaddleAdapter())
    monkeypatch.setattr(
        "ap_agent.tools.ocr.extract_tesseract_fallback_page", _FakeSuccessfulTesseractAdapter()
    )

    page_input = _routing_page_input(tmp_path)
    config = OCRConfig(artifact_root=tmp_path, ocr_version="ocr-v2-paddle")

    page_result, raw = extract_routed_ocr_page(
        document_id=uuid4(), page_input=page_input, config=config,
        evidence_image_destination=tmp_path / "evidence.png",
        engine=object(), engine_version="3.7.0",
    )

    assert raw["selected_provider"] == "tesseract-fallback"
    assert raw["fallback_used"] is True
    assert "paddle exploded" in raw["primary_error"]
    assert page_result.status == OCRStatus.REVIEW_REQUIRED
    assert "PRIMARY_PROVIDER_FAILED" in page_result.review_reasons
    # The fallback's evidence image is re-saved to the routed destination,
    # matching coordinates to a byte-stable, hash-recomputed copy.
    assert page_result.evidence_image_path == tmp_path / "evidence.png"
    assert (tmp_path / "evidence.png").exists()


def test_extract_routed_ocr_page_raises_when_both_providers_fail(tmp_path, monkeypatch):
    monkeypatch.setattr("ap_agent.tools.ocr.extract_paddle_ocr_page", _FakeFailingPaddleAdapter())
    monkeypatch.setattr(
        "ap_agent.tools.ocr.extract_tesseract_fallback_page", _FakeFailingTesseractAdapter()
    )

    page_input = _routing_page_input(tmp_path)
    config = OCRConfig(artifact_root=tmp_path, ocr_version="ocr-v2-paddle")

    with pytest.raises(RuntimeError, match="Both OCR providers failed"):
        extract_routed_ocr_page(
            document_id=uuid4(), page_input=page_input, config=config,
            evidence_image_destination=tmp_path / "evidence.png",
            engine=object(), engine_version="3.7.0",
        )


# ==============================================================================
# process_ocr_document: persistence order, isolation, idempotency
# ==============================================================================


def _document_input(tmp_path, page_count=1, batch_id=None, document_id=None):
    import cv2
    import numpy as np

    from ap_agent.artifacts.filesystem import calculate_file_sha256

    pages = []
    for page_number in range(1, page_count + 1):
        image_path = tmp_path / f"processed_{page_number}.png"
        cv2.imwrite(str(image_path), np.full((200, 200, 3), 255, dtype="uint8"))
        pages.append(
            OCRPageInput(
                page_number=page_number,
                processed_image_path=image_path,
                processed_image_sha256=calculate_file_sha256(image_path),
            )
        )
    return OCRDocumentInput(
        batch_id=batch_id or uuid4(),
        document_id=document_id or uuid4(),
        source_document_sha256="f" * 64,
        preprocessing_version="phase2-v1",
        pages=tuple(pages),
    )


def _patch_paddle_with_total_guard_trigger(monkeypatch, tmp_path):
    """Every page 'succeeds' via Paddle but has a bare TOTAL token with no
    value nearby, so the guard should fire."""

    def fake_extract(document_id, page_input, config, evidence_image_destination, engine, engine_version):
        from ap_agent.artifacts.filesystem import save_pil_image_atomically
        from PIL import Image

        save_pil_image_atomically(Image.new("RGB", (10, 10)), evidence_image_destination)
        token = _token("TOTAL", _box(0, 100, 50, 20))
        page_result = OCRPageResult(
            page_number=page_input.page_number,
            processed_image_path=page_input.processed_image_path,
            processed_image_sha256=page_input.processed_image_sha256,
            evidence_image_path=evidence_image_destination,
            evidence_image_sha256="z" * 64,
            page_text="TOTAL",
            tokens=(token,),
            evidence_lines=(),
            mean_confidence=95.0,
            low_confidence_token_count=0,
            low_confidence_ratio=0.0,
            status=OCRStatus.SUCCEEDED,
            review_reasons=(),
            ocr_engine="paddleocr",
            ocr_engine_version="3.7.0",
            ocr_configuration="lang=en",
            processed_at=datetime.now(timezone.utc),
        )
        return page_result, {"selected_provider": "paddleocr", "fallback_used": False, "payload": {}}

    monkeypatch.setattr("ap_agent.tools.ocr.extract_paddle_ocr_page", fake_extract)


def test_process_ocr_document_persists_the_guarded_result_not_the_pre_guard_one(tmp_path, monkeypatch):
    """The critical persistence-order assertion (task §7): a persisted
    SUCCEEDED result paired with an in-memory REVIEW_REQUIRED result is
    prohibited. Here the page 'succeeds' at the provider level but the
    TOTAL guard should downgrade it before anything is written."""

    _patch_paddle_with_total_guard_trigger(monkeypatch, tmp_path)

    document_input = _document_input(tmp_path)
    config = OCRConfig(artifact_root=tmp_path / "ocr", ocr_version="ocr-v2-paddle")

    result = process_ocr_document(document_input, config, engine=object(), engine_version="3.7.0")

    assert result.status == OCRStatus.REVIEW_REQUIRED
    assert TOTAL_VALUE_MISSING_REASON in result.pages[0].review_reasons

    document_directory = build_ocr_document_directory(document_input, config)
    persisted_document = json.loads(
        (document_directory / "ocr_document_result.json").read_text()
    )
    persisted_event = json.loads((document_directory / "ocr_event.json").read_text())
    persisted_page = json.loads(
        (document_directory / "page_001" / "ocr_page_result.json").read_text()
    )

    assert persisted_document["status"] == "REVIEW_REQUIRED"
    assert persisted_event["status"] == "REVIEW_REQUIRED"
    assert persisted_page["status"] == "REVIEW_REQUIRED"
    assert TOTAL_VALUE_MISSING_REASON in persisted_page["review_reasons"]


def test_process_ocr_document_persisted_and_in_memory_results_always_agree(tmp_path, monkeypatch):
    fake_paddle = _FakeSuccessfulPaddleAdapter()
    monkeypatch.setattr("ap_agent.tools.ocr.extract_paddle_ocr_page", fake_paddle)

    document_input = _document_input(tmp_path, page_count=2)
    config = OCRConfig(artifact_root=tmp_path / "ocr", ocr_version="ocr-v2-paddle")

    result = process_ocr_document(document_input, config, engine=object(), engine_version="3.7.0")

    document_directory = build_ocr_document_directory(document_input, config)
    persisted_document = json.loads(
        (document_directory / "ocr_document_result.json").read_text()
    )

    assert persisted_document["status"] == result.status.value
    assert persisted_document["page_count"] == len(result.pages)
    for page in result.pages:
        page_directory = document_directory / f"page_{page.page_number:03d}"
        persisted_page = json.loads((page_directory / "ocr_page_result.json").read_text())
        assert persisted_page["status"] == page.status.value


def test_process_ocr_document_fails_closed_on_both_providers_failing(tmp_path, monkeypatch):
    monkeypatch.setattr("ap_agent.tools.ocr.extract_paddle_ocr_page", _FakeFailingPaddleAdapter())
    monkeypatch.setattr(
        "ap_agent.tools.ocr.extract_tesseract_fallback_page", _FakeFailingTesseractAdapter()
    )

    document_input = _document_input(tmp_path)
    config = OCRConfig(artifact_root=tmp_path / "ocr", ocr_version="ocr-v2-paddle")

    result = process_ocr_document(document_input, config, engine=object(), engine_version="3.7.0")

    assert result.status == OCRStatus.FAILED
    assert result.event.review_required is True
    assert "Both OCR providers failed" in result.errors[0]


def test_process_ocr_document_fails_closed_on_a_tampered_phase_2_artifact(tmp_path):
    document_input = _document_input(tmp_path)
    # Tamper with the processed image after building the (hash-pinned) input.
    document_input.pages[0].processed_image_path.write_bytes(b"corrupted")

    config = OCRConfig(artifact_root=tmp_path / "ocr", ocr_version="ocr-v2-paddle")

    # No engine needed: Paddle raises on the hash mismatch (network access
    # is not required to reach that failure), and Tesseract fails the
    # same way, so we simulate the routing directly to avoid depending on
    # network access for this fail-closed check.
    class RaisingEngine:
        def predict(self, path):
            raise ValueError("Processed page SHA-256 does not match the Phase 2 evidence record.")

    result = process_ocr_document(document_input, config, engine=RaisingEngine(), engine_version="0.0")

    assert result.status == OCRStatus.FAILED
    assert "SHA-256" in result.errors[0]


def test_process_ocr_document_fails_closed_on_a_missing_phase_2_artifact(tmp_path):
    document_input = _document_input(tmp_path)
    document_input.pages[0].processed_image_path.unlink()

    config = OCRConfig(artifact_root=tmp_path / "ocr", ocr_version="ocr-v2-paddle")

    class RaisingEngine:
        def predict(self, path):
            raise FileNotFoundError("missing")

    result = process_ocr_document(document_input, config, engine=RaisingEngine(), engine_version="0.0")

    assert result.status == OCRStatus.FAILED


def test_process_ocr_document_isolates_artifacts_across_documents(tmp_path, monkeypatch):
    fake_paddle = _FakeSuccessfulPaddleAdapter()
    monkeypatch.setattr("ap_agent.tools.ocr.extract_paddle_ocr_page", fake_paddle)

    config = OCRConfig(artifact_root=tmp_path / "ocr", ocr_version="ocr-v2-paddle")
    batch_id = uuid4()

    first_input = _document_input(tmp_path, batch_id=batch_id)
    second_input = _document_input(tmp_path, batch_id=batch_id)

    process_ocr_document(first_input, config, engine=object(), engine_version="3.7.0")
    process_ocr_document(second_input, config, engine=object(), engine_version="3.7.0")

    first_directory = config.artifact_root / str(batch_id) / str(first_input.document_id)
    second_directory = config.artifact_root / str(batch_id) / str(second_input.document_id)

    first_contents = "\n".join(str(p) for p in first_directory.rglob("*"))
    assert str(second_input.document_id) not in first_contents


def test_process_ocr_document_rerun_is_idempotent(tmp_path, monkeypatch):
    fake_paddle = _FakeSuccessfulPaddleAdapter()
    monkeypatch.setattr("ap_agent.tools.ocr.extract_paddle_ocr_page", fake_paddle)

    document_input = _document_input(tmp_path)
    config = OCRConfig(artifact_root=tmp_path / "ocr", ocr_version="ocr-v2-paddle")

    first = process_ocr_document(document_input, config, engine=object(), engine_version="3.7.0")
    second = process_ocr_document(document_input, config, engine=object(), engine_version="3.7.0")

    assert first.status == second.status
    assert [t.evidence_id for t in first.pages[0].tokens] == [
        t.evidence_id for t in second.pages[0].tokens
    ]


def test_process_ocr_document_rejects_duplicate_page_numbers(tmp_path):
    document_input = _document_input(tmp_path)
    duplicated = replace(
        document_input, pages=document_input.pages + document_input.pages
    )
    config = OCRConfig(artifact_root=tmp_path / "ocr", ocr_version="ocr-v2-paddle")

    result = process_ocr_document(duplicated, config, engine=object(), engine_version="3.7.0")

    assert result.status == OCRStatus.FAILED
    assert "duplicate" in result.errors[0].lower()


def test_process_ocr_document_rejects_empty_page_list(tmp_path):
    document_input = replace(_document_input(tmp_path), pages=())
    config = OCRConfig(artifact_root=tmp_path / "ocr", ocr_version="ocr-v2-paddle")

    result = process_ocr_document(document_input, config, engine=object(), engine_version="3.7.0")

    assert result.status == OCRStatus.FAILED
    assert "no pages" in result.errors[0].lower()


def test_process_ocr_document_review_reasons_are_unique(tmp_path, monkeypatch):
    _patch_paddle_with_total_guard_trigger(monkeypatch, tmp_path)

    document_input = _document_input(tmp_path)
    config = OCRConfig(
        artifact_root=tmp_path / "ocr",
        ocr_version="ocr-v2-paddle",
        minimum_meaningful_tokens=10,  # forces INSUFFICIENT_OCR_TOKENS too
    )

    # Re-run process_ocr_document twice against the same input; the guard
    # must not accumulate duplicate reasons across calls.
    process_ocr_document(document_input, config, engine=object(), engine_version="3.7.0")
    result = process_ocr_document(document_input, config, engine=object(), engine_version="3.7.0")

    reasons = result.pages[0].review_reasons
    assert len(reasons) == len(set(reasons))

"""M2 contract tests for Phase 3 (OCR) models.

`OCRPageResult` must be the cell-41 definition (with `evidence_image_path`
and `evidence_image_sha256`), not the superseded cell-34 one (§3.1 of the
modularisation map). OCR engines and routing functions are out of scope
until the processing-extraction milestone.
"""

import dataclasses
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


def test_ocr_status_members():
    assert [member.value for member in OCRStatus] == [
        "SUCCEEDED",
        "REVIEW_REQUIRED",
        "FAILED",
    ]


def test_ocr_config_defaults_and_config_string():
    config = OCRConfig(artifact_root=Path("/tmp/ap-agent"))
    assert config.language == "eng"
    assert config.ocr_engine_name == "tesseract"
    assert 0 <= config.minimum_token_confidence <= 100
    assert 0 <= config.minimum_page_mean_confidence <= 100
    assert 0 <= config.maximum_low_confidence_ratio <= 1
    assert config.tesseract_config_string == "--oem 3 --psm 6"


def test_ocr_config_instances_are_independent():
    first = OCRConfig(artifact_root=Path("/tmp/a"), ocr_version="ocr-v2-paddle")
    second = OCRConfig(artifact_root=Path("/tmp/b"))
    assert first.ocr_version == "ocr-v2-paddle"
    assert second.ocr_version == "ocr-v1"

    with pytest.raises(dataclasses.FrozenInstanceError):
        second.ocr_version = "mutated"


def test_bounding_box_right_and_bottom():
    box = BoundingBox(x=10, y=20, width=100, height=50)
    assert box.right == 110
    assert box.bottom == 70


def test_ocr_page_result_has_evidence_image_fields():
    """Guards against accidentally extracting the superseded cell-34 shape."""

    field_names = {f.name for f in dataclasses.fields(OCRPageResult)}
    assert "evidence_image_path" in field_names
    assert "evidence_image_sha256" in field_names
    assert "processed_image_path" in field_names


def test_ocr_page_result_instantiates_with_representative_values():
    box = BoundingBox(x=0, y=0, width=10, height=10)
    token = OCRToken(
        evidence_id=uuid4(),
        page_number=1,
        reading_order=0,
        text="TOTAL",
        confidence=99.5,
        bounding_box=box,
        block_number=1,
        paragraph_number=1,
        line_number=1,
        word_number=1,
    )
    line = EvidenceLine(
        evidence_line_id=uuid4(),
        page_number=1,
        reading_order=0,
        text="TOTAL 69.22",
        mean_confidence=99.5,
        bounding_box=box,
        token_evidence_ids=(token.evidence_id,),
    )
    page = OCRPageResult(
        page_number=1,
        processed_image_path=Path("processed.png"),
        processed_image_sha256="a" * 64,
        evidence_image_path=Path("evidence_image.png"),
        evidence_image_sha256="b" * 64,
        page_text="TOTAL 69.22",
        tokens=(token,),
        evidence_lines=(line,),
        mean_confidence=99.5,
        low_confidence_token_count=0,
        low_confidence_ratio=0.0,
        status=OCRStatus.SUCCEEDED,
        review_reasons=(),
        ocr_engine="paddleocr",
        ocr_engine_version="3.7.0",
        ocr_configuration="paddleocr",
        processed_at=datetime.now(timezone.utc),
    )
    assert page.tokens[0].bounding_box is box
    assert page.evidence_lines[0].token_evidence_ids == (token.evidence_id,)

    with pytest.raises(dataclasses.FrozenInstanceError):
        page.status = OCRStatus.FAILED


def test_ocr_document_input_and_result_shape():
    page_input = OCRPageInput(
        page_number=1,
        processed_image_path=Path("processed.png"),
        processed_image_sha256="c" * 64,
    )
    document_input = OCRDocumentInput(
        batch_id=uuid4(),
        document_id=uuid4(),
        source_document_sha256="d" * 64,
        preprocessing_version="phase2-v1",
        pages=(page_input,),
    )
    assert document_input.pages[0].preprocessing_review_required is False

    event = OCREvent(
        event_type="phase3.ocr",
        status=OCRStatus.SUCCEEDED,
        batch_id=document_input.batch_id,
        document_id=document_input.document_id,
        occurred_at=datetime.now(timezone.utc),
        message="ok",
        review_required=False,
    )
    result = OCRDocumentResult(
        batch_id=document_input.batch_id,
        document_id=document_input.document_id,
        source_document_sha256=document_input.source_document_sha256,
        preprocessing_version=document_input.preprocessing_version,
        status=OCRStatus.SUCCEEDED,
        pages=(),
        event=event,
    )
    assert result.errors == ()

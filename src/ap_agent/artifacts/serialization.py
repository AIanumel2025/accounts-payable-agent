"""JSON-serialisation helpers for Phase 2 (preprocessing) and Phase 3 (OCR)
contracts.

Source: notebook cell 28 ("PHASE 2 — CELL 2") for the Phase 2 serialisers;
cell 38 ("PHASE 3 — CELL 3") for `bounding_box_to_dict`, `ocr_token_to_dict`,
`evidence_line_to_dict`, `ocr_event_to_dict`, `ocr_document_result_to_dict`;
cell 41 ("PHASE 3 — CORRECTION CELL 4B") for `make_json_compatible`; cell 43
("PHASE 3 — CORRECTION CELL 4C") for the final, active `ocr_page_result_to_dict`
(supersedes the cell-38 version by adding the `evidence_image_path`/
`evidence_image_sha256` fields, §3.1). Active-definition table §2
`artifacts/serialization.py`.

Extracted verbatim: field order, key names and value shapes (lists instead
of tuples, `.value` for enums, `.isoformat()` for timestamps) match the
notebook's own serialisers exactly, since these functions decide the
on-disk byte layout of every Phase 2/3 JSON artifact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from ap_agent.models.ocr import (
    BoundingBox,
    EvidenceLine,
    OCRDocumentResult,
    OCREvent,
    OCRPageResult,
    OCRToken,
)
from ap_agent.models.preprocessing import (
    PageQuality,
    PreprocessedPage,
    PreprocessingEvent,
)

__all__ = [
    "page_quality_to_dict",
    "preprocessing_event_to_dict",
    "preprocessed_page_to_dict",
    "make_json_compatible",
    "bounding_box_to_dict",
    "ocr_token_to_dict",
    "evidence_line_to_dict",
    "ocr_page_result_to_dict",
    "ocr_event_to_dict",
    "ocr_document_result_to_dict",
]


def page_quality_to_dict(
    quality: PageQuality,
) -> dict:
    return {
        "page_number": quality.page_number,
        "width": quality.width,
        "height": quality.height,
        "brightness_score": quality.brightness_score,
        "contrast_score": quality.contrast_score,
        "blur_score": quality.blur_score,
        "detected_skew_angle": quality.detected_skew_angle,
        "quality_flags": list(quality.quality_flags),
        "review_required": quality.review_required,
    }


def preprocessing_event_to_dict(
    event: PreprocessingEvent,
) -> dict:
    return {
        "event_type": event.event_type,
        "status": event.status.value,
        "batch_id": str(event.batch_id),
        "document_id": str(event.document_id),
        "occurred_at": event.occurred_at.isoformat(),
        "message": event.message,
        "review_required": event.review_required,
    }


def preprocessed_page_to_dict(
    page: PreprocessedPage,
) -> dict:
    return {
        "page_number": page.page_number,
        "original_render_path": str(
            page.original_render_path
        ),
        "processed_image_path": str(
            page.processed_image_path
        ),
        "original_render_sha256": (
            page.original_render_sha256
        ),
        "processed_image_sha256": (
            page.processed_image_sha256
        ),
        "quality": page_quality_to_dict(
            page.quality
        ),
    }


# ---------------------------------------------------------
# Phase 3 (OCR) serialisers
# ---------------------------------------------------------

def make_json_compatible(
    value: Any,
) -> Any:
    import numpy as np

    if isinstance(value, np.ndarray):
        return value.tolist()

    if isinstance(value, np.generic):
        return value.item()

    if isinstance(value, dict):
        return {
            str(key): make_json_compatible(
                nested_value
            )
            for key, nested_value
            in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [
            make_json_compatible(item)
            for item in value
        ]

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, UUID):
        return str(value)

    return value


def bounding_box_to_dict(
    bounding_box: BoundingBox,
) -> dict:
    return {
        "x": bounding_box.x,
        "y": bounding_box.y,
        "width": bounding_box.width,
        "height": bounding_box.height,
        "right": bounding_box.right,
        "bottom": bounding_box.bottom,
    }


def ocr_token_to_dict(
    token: OCRToken,
) -> dict:
    return {
        "evidence_id": str(
            token.evidence_id
        ),
        "page_number": token.page_number,
        "reading_order": token.reading_order,
        "text": token.text,
        "confidence": token.confidence,
        "bounding_box": bounding_box_to_dict(
            token.bounding_box
        ),
        "block_number": token.block_number,
        "paragraph_number": (
            token.paragraph_number
        ),
        "line_number": token.line_number,
        "word_number": token.word_number,
    }


def evidence_line_to_dict(
    evidence_line: EvidenceLine,
) -> dict:
    return {
        "evidence_line_id": str(
            evidence_line.evidence_line_id
        ),
        "page_number": (
            evidence_line.page_number
        ),
        "reading_order": (
            evidence_line.reading_order
        ),
        "text": evidence_line.text,
        "mean_confidence": (
            evidence_line.mean_confidence
        ),
        "bounding_box": bounding_box_to_dict(
            evidence_line.bounding_box
        ),
        "token_evidence_ids": [
            str(evidence_id)
            for evidence_id
            in evidence_line.token_evidence_ids
        ],
    }


def ocr_page_result_to_dict(
    page_result: OCRPageResult,
) -> dict:
    """Final, active serialiser (notebook cell 43).

    Supersedes the cell-38 version by adding `evidence_image_path` and
    `evidence_image_sha256` (§3.1 of the modularisation map), matching the
    cell-41 `OCRPageResult` contract shape.
    """

    return {
        "page_number": (
            page_result.page_number
        ),
        "processed_image_path": str(
            page_result.processed_image_path
        ),
        "processed_image_sha256": (
            page_result.processed_image_sha256
        ),
        "evidence_image_path": str(
            page_result.evidence_image_path
        ),
        "evidence_image_sha256": (
            page_result.evidence_image_sha256
        ),
        "page_text": page_result.page_text,
        "token_count": len(
            page_result.tokens
        ),
        "evidence_line_count": len(
            page_result.evidence_lines
        ),
        "mean_confidence": (
            page_result.mean_confidence
        ),
        "low_confidence_token_count": (
            page_result
            .low_confidence_token_count
        ),
        "low_confidence_ratio": (
            page_result.low_confidence_ratio
        ),
        "status": (
            page_result.status.value
        ),
        "review_reasons": list(
            page_result.review_reasons
        ),
        "ocr_engine": (
            page_result.ocr_engine
        ),
        "ocr_engine_version": (
            page_result.ocr_engine_version
        ),
        "ocr_configuration": (
            page_result.ocr_configuration
        ),
        "processed_at": (
            page_result.processed_at
            .isoformat()
        ),
    }


def ocr_event_to_dict(
    event: OCREvent,
) -> dict:
    return {
        "event_type": event.event_type,
        "status": event.status.value,
        "batch_id": str(event.batch_id),
        "document_id": str(
            event.document_id
        ),
        "occurred_at": (
            event.occurred_at.isoformat()
        ),
        "message": event.message,
        "review_required": (
            event.review_required
        ),
    }


def ocr_document_result_to_dict(
    result: OCRDocumentResult,
) -> dict:
    return {
        "batch_id": str(result.batch_id),
        "document_id": str(
            result.document_id
        ),
        "source_document_sha256": (
            result.source_document_sha256
        ),
        "preprocessing_version": (
            result.preprocessing_version
        ),
        "status": result.status.value,
        "page_count": len(result.pages),
        "pages": [
            ocr_page_result_to_dict(page)
            for page in result.pages
        ],
        "event": ocr_event_to_dict(
            result.event
        ),
        "errors": list(result.errors),
    }

"""JSON-serialisation helpers for Phase 2 (preprocessing) contracts.

Source: notebook cell 28 ("PHASE 2 — CELL 2"), active-definition table §2
`artifacts/serialization.py`. Extracted verbatim: field order, key names and
value shapes (lists instead of tuples, `.value` for enums, `.isoformat()`
for timestamps) match the notebook's own serialisers exactly, since these
functions decide the on-disk byte layout of every Phase 2 JSON artifact.
"""

from __future__ import annotations

from ap_agent.models.preprocessing import (
    PageQuality,
    PreprocessedPage,
    PreprocessingEvent,
)

__all__ = [
    "page_quality_to_dict",
    "preprocessing_event_to_dict",
    "preprocessed_page_to_dict",
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

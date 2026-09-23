"""Deterministic OCR evidence-ID and bounding-box helpers shared by both
OCR provider adapters.

Source: notebook cell 36 ("PHASE 3 — CELL 2"), active-definition table §2
`tools/ocr_evidence.py` *(new file, decision D-7)*. This module exists to
avoid the `tools/ocr` <-> `adapters/*` import cycle risk identified as C-1
in `docs/modularisation_map.md` §4.4: both OCR adapters need these four
helpers, and `tools/ocr` needs the adapters, so the helpers live in their
own leaf module (depends only on `models.ocr`) that both adapters import.

Extracted verbatim: identical identity-material formulas and validation
branch order to the notebook.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from ap_agent.models.ocr import BoundingBox

__all__ = [
    "create_token_evidence_id",
    "create_line_evidence_id",
    "combine_bounding_boxes",
    "validate_bounding_box",
]


def create_token_evidence_id(
    document_id: UUID,
    page_number: int,
    reading_order: int,
    text: str,
    bounding_box: BoundingBox,
    processed_image_sha256: str,
    ocr_version: str,
) -> UUID:
    identity_material = "|".join(
        [
            "ocr-token",
            str(document_id),
            str(page_number),
            str(reading_order),
            text,
            str(bounding_box.x),
            str(bounding_box.y),
            str(bounding_box.width),
            str(bounding_box.height),
            processed_image_sha256,
            ocr_version,
        ]
    )

    return uuid5(
        NAMESPACE_URL,
        identity_material,
    )


def create_line_evidence_id(
    document_id: UUID,
    page_number: int,
    reading_order: int,
    text: str,
    bounding_box: BoundingBox,
    processed_image_sha256: str,
    ocr_version: str,
) -> UUID:
    identity_material = "|".join(
        [
            "ocr-line",
            str(document_id),
            str(page_number),
            str(reading_order),
            text,
            str(bounding_box.x),
            str(bounding_box.y),
            str(bounding_box.width),
            str(bounding_box.height),
            processed_image_sha256,
            ocr_version,
        ]
    )

    return uuid5(
        NAMESPACE_URL,
        identity_material,
    )


def combine_bounding_boxes(
    bounding_boxes: list[BoundingBox],
) -> BoundingBox:
    if not bounding_boxes:
        raise ValueError(
            "At least one bounding box is required."
        )

    left = min(
        box.x
        for box in bounding_boxes
    )

    top = min(
        box.y
        for box in bounding_boxes
    )

    right = max(
        box.right
        for box in bounding_boxes
    )

    bottom = max(
        box.bottom
        for box in bounding_boxes
    )

    return BoundingBox(
        x=left,
        y=top,
        width=right - left,
        height=bottom - top,
    )


def validate_bounding_box(
    bounding_box: BoundingBox,
    image_width: int,
    image_height: int,
) -> None:
    if bounding_box.x < 0:
        raise ValueError(
            "Bounding-box x-coordinate is negative."
        )

    if bounding_box.y < 0:
        raise ValueError(
            "Bounding-box y-coordinate is negative."
        )

    if bounding_box.width <= 0:
        raise ValueError(
            "Bounding-box width must be positive."
        )

    if bounding_box.height <= 0:
        raise ValueError(
            "Bounding-box height must be positive."
        )

    if bounding_box.right > image_width:
        raise ValueError(
            "Bounding box exceeds image width."
        )

    if bounding_box.bottom > image_height:
        raise ValueError(
            "Bounding box exceeds image height."
        )

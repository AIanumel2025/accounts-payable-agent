"""JSON-serialisation helpers for Phase 2 (preprocessing), Phase 3 (OCR) and
Phase 4 (normalisation) contracts.

Source: notebook cell 28 ("PHASE 2 — CELL 2") for the Phase 2 serialisers;
cell 38 ("PHASE 3 — CELL 3") for `bounding_box_to_dict`, `ocr_token_to_dict`,
`evidence_line_to_dict`, `ocr_event_to_dict`, `ocr_document_result_to_dict`;
cell 41 ("PHASE 3 — CORRECTION CELL 4B") for `make_json_compatible`; cell 43
("PHASE 3 — CORRECTION CELL 4C") for the final, active `ocr_page_result_to_dict`
(supersedes the cell-38 version by adding the `evidence_image_path`/
`evidence_image_sha256` fields, §3.1); cell 63 ("PHASE 4 — CELL 5") for
`convert_to_json_safe`, the Phase 4 JSON-safety converter (active-definition
table §2 `artifacts/serialization.py`).

Extracted verbatim: field order, key names and value shapes (lists instead
of tuples, `.value` for enums, `.isoformat()` for timestamps) match the
notebook's own serialisers exactly, since these functions decide the
on-disk byte layout of every Phase 2/3/4 JSON artifact.

`convert_to_json_safe` is a distinct, generic converter from
`make_json_compatible`: it walks any dataclass tree (recursing through
`dataclasses.fields`) and additionally converts `Enum`, `Decimal`, `date`
and `datetime` values, which the Phase 4 contracts use and the Phase 2/3
serialisers above do not need (they build their dicts by hand, field by
field). It is the Phase 4 byte format (§3.2 of the modularisation map,
decision D-8) and must not be confused with `make_json_compatible`, which
serves the Phase 3 raw-provider-output payloads instead.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
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
    "convert_to_json_safe",
    "canonical_decimal_text",
    "phase_5_json_safe",
    "phase_6_json_safe",
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


# ---------------------------------------------------------
# Phase 4 (normalisation) JSON-safety converter
# ---------------------------------------------------------


def convert_to_json_safe(value: Any) -> Any:
    """Recursively convert a Phase 4 value tree into JSON-safe types.

    Source: notebook cell 63 ("PHASE 4 — CELL 5"). Branch order is
    verbatim: dataclasses recurse field by field, `Enum` -> `.value`,
    `UUID`/`Decimal`/`Path` -> `str`, `datetime`/`date` -> `.isoformat()`,
    dict keys are stringified (`Enum` keys use `.value` first), and
    `list`/`tuple`/`set` all become JSON lists.
    """

    if value is None:
        return None

    if is_dataclass(value):
        return {
            contract_field.name: convert_to_json_safe(
                getattr(value, contract_field.name)
            )
            for contract_field in fields(value)
        }

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, dict):
        return {
            str(getattr(key, "value", key)): convert_to_json_safe(nested_value)
            for key, nested_value in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [convert_to_json_safe(item) for item in value]

    return value


# ---------------------------------------------------------
# Phase 5 (financial validation) serialisers
# ---------------------------------------------------------


def canonical_decimal_text(value: Decimal | None) -> str | None:
    """Convert a Decimal into stable, non-scientific text (notebook cell 69,
    "PHASE 5 — CELL 2"). Used for every audit-text operand and for
    `expected_value`/`observed_value` on a `ValidationCheckResult`, so a
    Phase 5 monetary value round-trips through JSON exactly as it reads
    (`format(value, "f")`, never `str(value)`, which can fall back to
    scientific notation for extreme exponents)."""

    if value is None:
        return None

    return format(value, "f")


def phase_5_json_safe(value: Any) -> Any:
    """Convert Phase 5 objects into deterministic JSON-safe data (notebook
    cell 73, "PHASE 5 — CELL 6").

    Distinct from `convert_to_json_safe` above (Phase 4's converter): this
    is the Phase 5 byte format used by `write_json_atomic`/
    `write_jsonl_atomic` (`ap_agent.artifacts.filesystem`) --- branch order,
    `Decimal` handling (`canonical_decimal_text`, not `str`), `set` handling
    (sorted into a list) and the `TypeError` fail-closed branch for any
    unsupported type are all verbatim from the notebook's own converter, and
    must not be merged with `convert_to_json_safe` (D-8-style policy: two
    behaviourally distinct converters, kept under distinct names)."""

    if value is None:
        return None

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, Decimal):
        return canonical_decimal_text(value)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, Path):
        return str(value)

    if is_dataclass(value):
        return {
            dataclass_field.name: phase_5_json_safe(getattr(value, dataclass_field.name))
            for dataclass_field in fields(value)
        }

    if isinstance(value, dict):
        return {str(key): phase_5_json_safe(item) for key, item in value.items()}

    if isinstance(value, (tuple, list)):
        return [phase_5_json_safe(item) for item in value]

    if isinstance(value, set):
        return sorted(phase_5_json_safe(item) for item in value)

    if isinstance(value, (str, int, float, bool)):
        return value

    raise TypeError(f"Unsupported Phase 5 serialization type: {type(value).__name__}")


# ---------------------------------------------------------
# Phase 6 (matching) serialiser
# ---------------------------------------------------------


def phase_6_json_safe(value: Any) -> Any:
    """Convert Phase 6 objects into JSON-safe data (notebook cell 82,
    "PHASE 6 — CELL 5", `phase_6_json_safe`).

    A fourth, distinct converter alongside `convert_to_json_safe` (Phase 4)
    and `phase_5_json_safe` (Phase 5) -- D-8-style policy: kept under its
    own name rather than merged, even though it is close to `phase_5_json_safe`
    (branch order and `Decimal`/date/UUID handling match). It differs in two
    verbatim, notebook-native ways that must not be "cleaned up" away: sets
    are converted to an *unsorted* list (`phase_5_json_safe` sorts them),
    and there is no `TypeError` fail-closed branch for an unsupported type
    -- an unmatched value is returned as-is, exactly as the notebook wrote
    it.
    """

    if value is None:
        return None

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, Decimal):
        return format(value, "f")

    if isinstance(value, (date, datetime)):
        return value.isoformat()

    if isinstance(value, Path):
        return str(value)

    if is_dataclass(value):
        return {
            dataclass_field.name: phase_6_json_safe(getattr(value, dataclass_field.name))
            for dataclass_field in fields(value)
        }

    if isinstance(value, dict):
        return {str(key): phase_6_json_safe(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [phase_6_json_safe(item) for item in value]

    return value

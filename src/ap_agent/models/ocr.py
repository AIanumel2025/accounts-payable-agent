"""Phase 3 (OCR) contracts: tokens, evidence lines and document results.

Source: notebook cell 34 ("PHASE 3 — CELL 1") for everything except
`OCRPageResult`. `OCRPageResult` is extracted from cell 41 ("PHASE 3 —
CORRECTION CELL 4B"), which superseded the cell-34 definition by adding
`evidence_image_path` and `evidence_image_sha256` (§3.1 of the
modularisation map). `OCRConfig` is a configuration dataclass and lives in
`ap_agent.config.settings` instead of here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from uuid import UUID

__all__ = [
    "OCRStatus",
    "OCRPageInput",
    "OCRDocumentInput",
    "BoundingBox",
    "OCRToken",
    "EvidenceLine",
    "OCRPageResult",
    "OCREvent",
    "OCRDocumentResult",
]


class OCRStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class OCRPageInput:
    page_number: int
    processed_image_path: Path
    processed_image_sha256: str
    preprocessing_review_required: bool = False


@dataclass(frozen=True)
class OCRDocumentInput:
    batch_id: UUID
    document_id: UUID
    source_document_sha256: str
    preprocessing_version: str
    pages: tuple[OCRPageInput, ...]


@dataclass(frozen=True)
class BoundingBox:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


@dataclass(frozen=True)
class OCRToken:
    evidence_id: UUID
    page_number: int
    reading_order: int

    text: str
    confidence: float

    bounding_box: BoundingBox

    block_number: int
    paragraph_number: int
    line_number: int
    word_number: int


@dataclass(frozen=True)
class EvidenceLine:
    evidence_line_id: UUID
    page_number: int
    reading_order: int

    text: str
    mean_confidence: float

    bounding_box: BoundingBox
    token_evidence_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class OCRPageResult:
    page_number: int

    # Phase 2 input
    processed_image_path: Path
    processed_image_sha256: str

    # Exact image used for OCR evidence coordinates.
    # This may be PaddleOCR's unwarped derivative.
    evidence_image_path: Path
    evidence_image_sha256: str

    page_text: str
    tokens: tuple[OCRToken, ...]
    evidence_lines: tuple[EvidenceLine, ...]

    mean_confidence: float
    low_confidence_token_count: int
    low_confidence_ratio: float

    status: OCRStatus
    review_reasons: tuple[str, ...]

    ocr_engine: str
    ocr_engine_version: str
    ocr_configuration: str

    processed_at: datetime


@dataclass(frozen=True)
class OCREvent:
    event_type: str
    status: OCRStatus

    batch_id: UUID
    document_id: UUID

    occurred_at: datetime
    message: str
    review_required: bool


@dataclass(frozen=True)
class OCRDocumentResult:
    batch_id: UUID
    document_id: UUID

    source_document_sha256: str
    preprocessing_version: str

    status: OCRStatus
    pages: tuple[OCRPageResult, ...]

    event: OCREvent
    errors: tuple[str, ...] = field(default_factory=tuple)

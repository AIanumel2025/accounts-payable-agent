"""Phase 2 (preprocessing) contracts.

Source: notebook cell 26 ("PHASE 2 — CELL 1"), active-definition table §2
`models/preprocessing.py`. `PreprocessingConfig` is a configuration
dataclass and lives in `ap_agent.config.settings` instead of here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from uuid import UUID

__all__ = [
    "PreprocessingStatus",
    "PreprocessingInput",
    "PageQuality",
    "PreprocessedPage",
    "PreprocessingEvent",
    "PreprocessingResult",
]


class PreprocessingStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class PreprocessingInput:
    batch_id: UUID
    document_id: UUID
    source_path: Path
    source_sha256: str


@dataclass(frozen=True)
class PageQuality:
    page_number: int
    width: int
    height: int
    brightness_score: float
    contrast_score: float
    blur_score: float
    detected_skew_angle: float
    quality_flags: tuple[str, ...]
    review_required: bool


@dataclass(frozen=True)
class PreprocessedPage:
    page_number: int
    original_render_path: Path
    processed_image_path: Path
    original_render_sha256: str
    processed_image_sha256: str
    quality: PageQuality


@dataclass(frozen=True)
class PreprocessingEvent:
    event_type: str
    status: PreprocessingStatus
    batch_id: UUID
    document_id: UUID
    occurred_at: datetime
    message: str
    review_required: bool


@dataclass(frozen=True)
class PreprocessingResult:
    batch_id: UUID
    document_id: UUID
    source_path: Path
    source_sha256: str
    status: PreprocessingStatus
    pages: tuple[PreprocessedPage, ...]
    event: PreprocessingEvent
    errors: tuple[str, ...] = field(default_factory=tuple)

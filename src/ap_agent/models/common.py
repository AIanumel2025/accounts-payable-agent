"""Core cross-phase contracts.

Source: notebook cells 3, 4 and 5 (validated in cells 7-8), active-definition
table §2 `models/common.py`.

`ReviewReason`, `BatchRecord` and `ReviewRequest` have no caller in the
validated Phase 1-5 call graph; they are extracted anyway because they are
part of the validated core contract surface (Q-7).
"""

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "utc_now",
    "ProcessingStage",
    "ProcessingStatus",
    "ReviewReason",
    "BatchRecord",
    "DocumentRecord",
    "ProcessingEvent",
    "ReviewRequest",
]


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class ProcessingStage(str, Enum):
    INGESTION = "INGESTION"
    PREPROCESSING = "PREPROCESSING"
    OCR = "OCR"
    NORMALIZATION = "NORMALIZATION"
    VALIDATION = "VALIDATION"
    MATCHING = "MATCHING"
    PERSISTENCE = "PERSISTENCE"
    LLM_RECOMMENDATION = "LLM_RECOMMENDATION"
    ROUTING = "ROUTING"


class ProcessingStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SKIPPED = "SKIPPED"


class ReviewReason(str, Enum):
    INVALID_DOCUMENT = "INVALID_DOCUMENT"
    OCR_FAILED = "OCR_FAILED"
    OCR_LOW_CONFIDENCE = "OCR_LOW_CONFIDENCE"
    REQUIRED_FIELD_MISSING = "REQUIRED_FIELD_MISSING"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"
    TAX_MISMATCH = "TAX_MISMATCH"
    DUPLICATE_SUSPECTED = "DUPLICATE_SUSPECTED"
    SUPPLIER_NOT_FOUND = "SUPPLIER_NOT_FOUND"
    PO_NOT_FOUND = "PO_NOT_FOUND"
    MATCH_OUTSIDE_TOLERANCE = "MATCH_OUTSIDE_TOLERANCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    POLICY_EXCEPTION = "POLICY_EXCEPTION"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"


class BatchRecord(BaseModel):
    """A collection of independently processed documents."""

    model_config = ConfigDict(extra="forbid")

    batch_id: UUID = Field(default_factory=uuid4)
    source: str
    created_at: datetime = Field(default_factory=utc_now)
    status: ProcessingStatus = ProcessingStatus.PENDING
    document_count: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentRecord(BaseModel):
    """The processing identity and source metadata for one document."""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID = Field(default_factory=uuid4)
    batch_id: UUID
    original_filename: str
    source_path: Path
    media_type: str
    received_at: datetime = Field(default_factory=utc_now)
    status: ProcessingStatus = ProcessingStatus.PENDING
    sha256: str | None = None
    page_count: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProcessingEvent(BaseModel):
    """A traceable event produced by one pipeline stage."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    batch_id: UUID
    document_id: UUID
    stage: ProcessingStage
    status: ProcessingStatus
    occurred_at: datetime = Field(default_factory=utc_now)
    message: str
    error_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ReviewRequest(BaseModel):
    """A fail-closed request for human review."""

    model_config = ConfigDict(extra="forbid")

    review_id: UUID = Field(default_factory=uuid4)
    batch_id: UUID
    document_id: UUID
    reasons: list[ReviewReason] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    summary: str
    blocking: bool = True
    evidence_ids: list[UUID] = Field(default_factory=list)

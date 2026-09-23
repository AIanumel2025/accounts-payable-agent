"""Phase 1 (ingestion) contracts.

Source: notebook cell 10, active-definition table §2 `models/ingestion.py`.

`IngestionConfig` is a configuration dataclass and lives in
`ap_agent.config.settings` instead of here (see §6.1 of the modularisation
map). The UUID namespaces, supported-type tables and ingestion functions
stay deferred to the processing-extraction milestone (`tools/ingestion.py`).
"""

from enum import Enum
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ap_agent.models.common import DocumentRecord, ProcessingEvent

__all__ = [
    "IngestionDisposition",
    "IngestionErrorCode",
    "IntakeInspection",
    "DocumentIdentity",
    "IngestionResult",
]


class IngestionDisposition(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"


class IngestionErrorCode(str, Enum):
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    NOT_A_FILE = "NOT_A_FILE"
    EMPTY_FILE = "EMPTY_FILE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_EXTENSION = "UNSUPPORTED_EXTENSION"
    UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"
    EXTENSION_CONTENT_MISMATCH = "EXTENSION_CONTENT_MISMATCH"
    HASH_MISMATCH = "HASH_MISMATCH"
    STORAGE_FAILED = "STORAGE_FAILED"


class IntakeInspection(BaseModel):
    """Validated technical information about an incoming file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_path: Path
    original_filename: str
    extension: str
    media_type: str
    file_size_bytes: int = Field(gt=0)


class DocumentIdentity(BaseModel):
    """Immutable content and processing identity for a document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: UUID
    document_id: UUID
    content_id: UUID
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class IngestionResult(BaseModel):
    """Complete output returned by successful ingestion."""

    model_config = ConfigDict(extra="forbid")

    disposition: IngestionDisposition
    inspection: IntakeInspection
    identity: DocumentIdentity
    document: DocumentRecord
    event: ProcessingEvent
    stored_path: Path
    original_preserved: bool

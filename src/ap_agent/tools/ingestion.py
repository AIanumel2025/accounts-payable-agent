"""Phase 1 (ingestion) processing functions.

Source: notebook cells 12 and 14 (`docs/modularisation_map.md` §2
`tools/ingestion.py`, §1.4 deterministic-identifier formulas). These are the
only active, validated definitions of these names (no cell later than 14
redefines any of them; §3.1/§3.2 list no Phase 1 collision), extracted
verbatim: same names, same signatures, same branch order, same message
strings and error codes, same identifier formulas.

The UUID namespaces and the supported-type tables are also from cell 10
(alongside the Phase 1 contracts already extracted into
`ap_agent.models.ingestion`, `ap_agent.config.settings` and
`ap_agent.exceptions` during M2); the modularisation map places them here,
in `tools/ingestion.py`, rather than in the contracts module (§1.3).

No function here depends on notebook state: `IngestionConfig`, the batch ID,
the ingestion source label and the set of known SHA-256 values are all
explicit parameters, exactly as the notebook already had them (Phase 1 was
never a hidden-global phase; §5.1 lists no Phase 1 entry here).
"""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from ap_agent.config.settings import IngestionConfig
from ap_agent.exceptions import IngestionValidationError
from ap_agent.models.common import (
    DocumentRecord,
    ProcessingEvent,
    ProcessingStage,
    ProcessingStatus,
)
from ap_agent.models.ingestion import (
    DocumentIdentity,
    IngestionDisposition,
    IngestionErrorCode,
    IngestionResult,
    IntakeInspection,
)

__all__ = [
    "BATCH_NAMESPACE",
    "CONTENT_NAMESPACE",
    "DOCUMENT_NAMESPACE",
    "SUPPORTED_DOCUMENT_TYPES",
    "CANONICAL_EXTENSION_BY_MEDIA_TYPE",
    "create_batch_id",
    "detect_document_media_type",
    "validate_intake",
    "calculate_sha256",
    "validate_sha256",
    "build_document_identity",
    "is_exact_duplicate",
    "preserve_original_document",
    "build_document_record",
    "ingest_document",
]


# Stable namespaces
BATCH_NAMESPACE = UUID("c251d18d-b096-47eb-908d-a86a250b2c50")
CONTENT_NAMESPACE = UUID("40c5b702-ffcf-4942-8e62-18b543f1d9ea")
DOCUMENT_NAMESPACE = UUID("63c9eed4-32bc-4340-979c-e89bd9276409")

# Supported file types. Will add more to accommdate customer preferences
SUPPORTED_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

CANONICAL_EXTENSION_BY_MEDIA_TYPE = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
}


def create_batch_id(
    source: str,
    external_reference: str | None = None
) -> UUID:
    """
    Create a batch ID.

    With an external reference, retries receive the same batch ID.
    Without one, a new random batch ID is created.
    """
    normalized_source = source.strip().lower()

    if not normalized_source:
        raise ValueError("Batch source must not be empty.")

    if external_reference is None:
        return uuid4()

    normalized_reference = external_reference.strip().lower()

    if not normalized_reference:
        raise ValueError(
            "External reference must not be blank when supplied."
        )

    return uuid5(
        BATCH_NAMESPACE,
        f"{normalized_source}:{normalized_reference}"
    )


def detect_document_media_type(file_path: str | Path) -> str:
    """Detect a supported file type using its binary signature."""
    path = Path(file_path)

    with path.open("rb") as file_handle:
        signature = file_handle.read(16)

    if signature.startswith(b"%PDF-"):
        return "application/pdf"

    if signature.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    if signature.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    if (
        signature.startswith(b"II*\x00")
        or signature.startswith(b"MM\x00*")
    ):
        return "image/tiff"

    raise IngestionValidationError(
        code=IngestionErrorCode.UNSUPPORTED_CONTENT,
        message="The file does not contain a supported document signature.",
        details={"path": str(path)}
    )


def validate_intake(
    file_path: str | Path,
    config: IngestionConfig
) -> IntakeInspection:
    """Validate the path, size, extension and actual file content."""
    path = Path(file_path)

    if not path.exists():
        raise IngestionValidationError(
            code=IngestionErrorCode.FILE_NOT_FOUND,
            message=f"Document does not exist: {path}",
            details={"path": str(path)}
        )

    if not path.is_file():
        raise IngestionValidationError(
            code=IngestionErrorCode.NOT_A_FILE,
            message=f"Expected a file but received: {path}",
            details={"path": str(path)}
        )

    file_size = path.stat().st_size

    if file_size == 0:
        raise IngestionValidationError(
            code=IngestionErrorCode.EMPTY_FILE,
            message=f"Document is empty: {path}",
            details={"path": str(path)}
        )

    if file_size > config.maximum_file_size_bytes:
        raise IngestionValidationError(
            code=IngestionErrorCode.FILE_TOO_LARGE,
            message="Document exceeds the configured file-size limit.",
            details={
                "path": str(path),
                "file_size_bytes": file_size,
                "maximum_file_size_bytes": (
                    config.maximum_file_size_bytes
                )
            }
        )

    extension = path.suffix.lower()

    if extension not in SUPPORTED_DOCUMENT_TYPES:
        raise IngestionValidationError(
            code=IngestionErrorCode.UNSUPPORTED_EXTENSION,
            message=f"Unsupported document extension: {extension}",
            details={
                "path": str(path),
                "extension": extension
            }
        )

    detected_media_type = detect_document_media_type(path)
    expected_media_type = SUPPORTED_DOCUMENT_TYPES[extension]

    if detected_media_type != expected_media_type:
        raise IngestionValidationError(
            code=IngestionErrorCode.EXTENSION_CONTENT_MISMATCH,
            message="The extension does not match the document content.",
            details={
                "path": str(path),
                "expected_media_type": expected_media_type,
                "detected_media_type": detected_media_type
            }
        )

    return IntakeInspection(
        source_path=path.resolve(),
        original_filename=path.name,
        extension=extension,
        media_type=detected_media_type,
        file_size_bytes=file_size
    )


def calculate_sha256(
    file_path: str | Path,
    chunk_size: int = 1024 * 1024
) -> str:
    """Calculate SHA-256 without loading the complete file into memory."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero.")

    digest = hashlib.sha256()

    with Path(file_path).open("rb") as file_handle:
        while chunk := file_handle.read(chunk_size):
            digest.update(chunk)

    return digest.hexdigest()


def validate_sha256(sha256: str) -> str:
    """Validate and normalize a SHA-256 digest."""
    normalized_hash = sha256.strip().lower()

    if len(normalized_hash) != 64:
        raise ValueError(
            "SHA-256 digest must contain exactly 64 characters."
        )

    try:
        int(normalized_hash, 16)
    except ValueError as exc:
        raise ValueError(
            "SHA-256 digest must contain only hexadecimal characters."
        ) from exc

    return normalized_hash


def build_document_identity(
    batch_id: UUID,
    sha256: str
) -> DocumentIdentity:
    """
    Generate stable content and document identities.

    content_id:
        Same for identical bytes across all batches.

    document_id:
        Same for identical bytes inside the same batch.
    """
    normalized_hash = validate_sha256(sha256)

    content_id = uuid5(
        CONTENT_NAMESPACE,
        normalized_hash
    )

    document_id = uuid5(
        DOCUMENT_NAMESPACE,
        f"{batch_id}:{normalized_hash}"
    )

    return DocumentIdentity(
        batch_id=batch_id,
        document_id=document_id,
        content_id=content_id,
        sha256=normalized_hash
    )


def is_exact_duplicate(
    candidate_sha256: str,
    known_sha256_values: set[str]
) -> bool:
    """Check whether byte-identical content is already known."""
    normalized_candidate = validate_sha256(candidate_sha256)

    normalized_known = {
        validate_sha256(known_hash)
        for known_hash in known_sha256_values
    }

    return normalized_candidate in normalized_known


def preserve_original_document(
    source_path: str | Path,
    identity: DocumentIdentity,
    inspection: IntakeInspection,
    config: IngestionConfig
) -> tuple[Path, bool]:
    """
    Copy the original into content-addressed storage.

    Returns:
        stored_path: Final preserved artifact path.
        created: True if newly copied, False if already present.
    """
    source = Path(source_path)

    destination_directory = (
        config.artifact_root
        / "originals"
        / str(identity.content_id)
    )

    destination_directory.mkdir(parents=True, exist_ok=True)

    canonical_extension = CANONICAL_EXTENSION_BY_MEDIA_TYPE[
        inspection.media_type
    ]

    destination_path = (
        destination_directory
        / f"original{canonical_extension}"
    )

    # Reuse an existing verified original.
    if destination_path.exists():
        existing_hash = calculate_sha256(destination_path)

        if existing_hash != identity.sha256:
            raise IngestionValidationError(
                code=IngestionErrorCode.HASH_MISMATCH,
                message=(
                    "The stored artifact does not match its "
                    "expected content identity."
                ),
                details={
                    "destination_path": str(destination_path),
                    "expected_sha256": identity.sha256,
                    "actual_sha256": existing_hash
                }
            )

        return destination_path.resolve(), False

    temporary_path: Path | None = None

    try:
        # Copy to a temporary path first so partial files are not exposed.
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination_directory,
            prefix=".ingestion-",
            suffix=".tmp",
            delete=False
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

            with source.open("rb") as source_file:
                shutil.copyfileobj(source_file, temporary_file)

        copied_hash = calculate_sha256(temporary_path)

        if copied_hash != identity.sha256:
            raise IngestionValidationError(
                code=IngestionErrorCode.HASH_MISMATCH,
                message="Copied document failed its integrity check.",
                details={
                    "expected_sha256": identity.sha256,
                    "actual_sha256": copied_hash
                }
            )

        # Atomic promotion from temporary to final artifact.
        os.replace(temporary_path, destination_path)

        return destination_path.resolve(), True

    except IngestionValidationError:
        raise

    except Exception as exc:
        raise IngestionValidationError(
            code=IngestionErrorCode.STORAGE_FAILED,
            message="The original document could not be preserved.",
            details={
                "source_path": str(source),
                "destination_path": str(destination_path),
                "error_type": type(exc).__name__
            }
        ) from exc

    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def build_document_record(
    inspection: IntakeInspection,
    identity: DocumentIdentity,
    stored_path: Path,
    ingestion_source: str
) -> DocumentRecord:
    """Create the standard document record for downstream stages."""
    return DocumentRecord(
        document_id=identity.document_id,
        batch_id=identity.batch_id,
        original_filename=inspection.original_filename,
        source_path=inspection.source_path,
        media_type=inspection.media_type,
        status=ProcessingStatus.PENDING,
        sha256=identity.sha256,
        metadata={
            "content_id": str(identity.content_id),
            "stored_original_path": str(stored_path),
            "file_size_bytes": inspection.file_size_bytes,
            "ingestion_source": ingestion_source
        }
    )


def ingest_document(
    file_path: str | Path,
    batch_id: UUID,
    ingestion_source: str,
    known_sha256_values: set[str],
    config: IngestionConfig
) -> IngestionResult:
    """
    Execute the complete ingestion stage for one document.
    """
    if not ingestion_source.strip():
        raise ValueError("ingestion_source must not be empty.")

    # 1. Validate the incoming file.
    inspection = validate_intake(
        file_path=file_path,
        config=config
    )

    # 2. Fingerprint the exact content.
    sha256 = calculate_sha256(inspection.source_path)

    # 3. Generate stable identities.
    identity = build_document_identity(
        batch_id=batch_id,
        sha256=sha256
    )

    # 4. Check for an exact duplicate.
    duplicate = is_exact_duplicate(
        candidate_sha256=sha256,
        known_sha256_values=known_sha256_values
    )

    # 5. Preserve or safely reuse the original.
    stored_path, original_preserved = preserve_original_document(
        source_path=inspection.source_path,
        identity=identity,
        inspection=inspection,
        config=config
    )

    # 6. Build the document registration record.
    document = build_document_record(
        inspection=inspection,
        identity=identity,
        stored_path=stored_path,
        ingestion_source=ingestion_source
    )

    # 7. Record the ingestion outcome.
    if duplicate:
        disposition = IngestionDisposition.DUPLICATE
        event_status = ProcessingStatus.SKIPPED
        event_message = (
            "Exact duplicate detected. Existing content reused."
        )
    else:
        disposition = IngestionDisposition.ACCEPTED
        event_status = ProcessingStatus.SUCCEEDED
        event_message = "Document ingestion completed successfully."

    event = ProcessingEvent(
        batch_id=batch_id,
        document_id=identity.document_id,
        stage=ProcessingStage.INGESTION,
        status=event_status,
        message=event_message,
        details={
            "disposition": disposition.value,
            "sha256": identity.sha256,
            "content_id": str(identity.content_id),
            "stored_path": str(stored_path),
            "original_preserved": original_preserved
        }
    )

    # 8. Return one typed result.
    return IngestionResult(
        disposition=disposition,
        inspection=inspection,
        identity=identity,
        document=document,
        event=event,
        stored_path=stored_path,
        original_preserved=original_preserved
    )

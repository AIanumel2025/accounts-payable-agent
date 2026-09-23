"""M2 contract tests for core (cells 3-5) and Phase 1 (cell 10) models.

These tests exercise contracts only: enum membership, dataclass/pydantic
field shape, defaults and immutability. Ingestion *functions* (cells 12,
14) are out of scope until the processing-extraction milestone.
"""

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ap_agent.config.settings import IngestionConfig
from ap_agent.exceptions import IngestionValidationError
from ap_agent.models.common import (
    BatchRecord,
    DocumentRecord,
    ProcessingEvent,
    ProcessingStage,
    ProcessingStatus,
    ReviewReason,
    ReviewRequest,
    utc_now,
)
from ap_agent.models.ingestion import (
    DocumentIdentity,
    IngestionDisposition,
    IngestionErrorCode,
    IngestionResult,
    IntakeInspection,
)


def test_processing_stage_members():
    assert [member.value for member in ProcessingStage] == [
        "INGESTION",
        "PREPROCESSING",
        "OCR",
        "NORMALIZATION",
        "VALIDATION",
        "MATCHING",
        "PERSISTENCE",
        "LLM_RECOMMENDATION",
        "ROUTING",
    ]


def test_processing_status_members():
    assert [member.value for member in ProcessingStatus] == [
        "PENDING",
        "IN_PROGRESS",
        "SUCCEEDED",
        "FAILED",
        "REVIEW_REQUIRED",
        "SKIPPED",
    ]


def test_review_reason_has_thirteen_members():
    assert len(ReviewReason) == 13
    assert ReviewReason.TOTAL_MISMATCH.value == "TOTAL_MISMATCH"


def test_ingestion_disposition_and_error_code_members():
    assert {member.value for member in IngestionDisposition} == {
        "ACCEPTED",
        "DUPLICATE",
    }
    assert {member.value for member in IngestionErrorCode} == {
        "FILE_NOT_FOUND",
        "NOT_A_FILE",
        "EMPTY_FILE",
        "FILE_TOO_LARGE",
        "UNSUPPORTED_EXTENSION",
        "UNSUPPORTED_CONTENT",
        "EXTENSION_CONTENT_MISMATCH",
        "HASH_MISMATCH",
        "STORAGE_FAILED",
    }


def test_batch_record_defaults():
    batch = BatchRecord(source="unit-test")
    assert batch.status == ProcessingStatus.PENDING
    assert batch.document_count == 0
    assert batch.metadata == {}
    assert batch.created_at.tzinfo is not None


def test_document_record_optional_fields_default_none():
    document = DocumentRecord(
        batch_id=uuid4(),
        original_filename="invoice.pdf",
        source_path=Path("invoice.pdf"),
        media_type="application/pdf",
    )
    assert document.status == ProcessingStatus.PENDING
    assert document.sha256 is None
    assert document.page_count is None


def test_processing_event_requires_stage_and_status():
    event = ProcessingEvent(
        batch_id=uuid4(),
        document_id=uuid4(),
        stage=ProcessingStage.INGESTION,
        status=ProcessingStatus.SUCCEEDED,
        message="ok",
    )
    assert event.error_code is None
    assert event.details == {}


def test_review_request_requires_at_least_one_reason():
    with pytest.raises(ValidationError):
        ReviewRequest(
            batch_id=uuid4(),
            document_id=uuid4(),
            reasons=[],
            summary="empty reasons must be rejected",
        )

    request = ReviewRequest(
        batch_id=uuid4(),
        document_id=uuid4(),
        reasons=[ReviewReason.OCR_FAILED],
        summary="one reason is enough",
    )
    assert request.blocking is True
    assert request.evidence_ids == []


def test_extra_fields_are_forbidden_on_core_models():
    with pytest.raises(ValidationError):
        BatchRecord(source="x", unexpected="not allowed")


def test_ingestion_config_defaults_and_frozen():
    config = IngestionConfig(artifact_root=Path("/tmp/ap-agent-artifacts"))
    assert config.maximum_file_size_bytes == 25 * 1024 * 1024

    with pytest.raises(ValidationError):
        config.maximum_file_size_bytes = 1


def test_ingestion_config_instances_are_independent():
    first = IngestionConfig(artifact_root=Path("/tmp/a"), maximum_file_size_bytes=10)
    second = IngestionConfig(artifact_root=Path("/tmp/b"))
    assert first.maximum_file_size_bytes == 10
    assert second.maximum_file_size_bytes == 25 * 1024 * 1024
    assert first.artifact_root != second.artifact_root


def test_intake_inspection_and_document_identity_instantiate():
    inspection = IntakeInspection(
        source_path=Path("invoice.pdf"),
        original_filename="invoice.pdf",
        extension=".pdf",
        media_type="application/pdf",
        file_size_bytes=1024,
    )
    identity = DocumentIdentity(
        batch_id=uuid4(),
        document_id=uuid4(),
        content_id=uuid4(),
        sha256="0" * 64,
    )
    assert inspection.file_size_bytes == 1024
    assert len(identity.sha256) == 64


def test_document_identity_rejects_bad_sha256():
    with pytest.raises(ValidationError):
        DocumentIdentity(
            batch_id=uuid4(),
            document_id=uuid4(),
            content_id=uuid4(),
            sha256="not-a-hash",
        )


def test_ingestion_result_assembles_the_full_contract():
    batch_id = uuid4()
    document_id = uuid4()
    inspection = IntakeInspection(
        source_path=Path("invoice.pdf"),
        original_filename="invoice.pdf",
        extension=".pdf",
        media_type="application/pdf",
        file_size_bytes=2048,
    )
    identity = DocumentIdentity(
        batch_id=batch_id,
        document_id=document_id,
        content_id=uuid4(),
        sha256="a" * 64,
    )
    document = DocumentRecord(
        document_id=document_id,
        batch_id=batch_id,
        original_filename="invoice.pdf",
        source_path=Path("invoice.pdf"),
        media_type="application/pdf",
        status=ProcessingStatus.SUCCEEDED,
    )
    event = ProcessingEvent(
        batch_id=batch_id,
        document_id=document_id,
        stage=ProcessingStage.INGESTION,
        status=ProcessingStatus.SUCCEEDED,
        message="accepted",
    )
    result = IngestionResult(
        disposition=IngestionDisposition.ACCEPTED,
        inspection=inspection,
        identity=identity,
        document=document,
        event=event,
        stored_path=Path("/artifacts/originals/a/original.pdf"),
        original_preserved=True,
    )
    assert result.disposition is IngestionDisposition.ACCEPTED
    assert result.original_preserved is True


def test_ingestion_validation_error_to_dict():
    error = IngestionValidationError(
        IngestionErrorCode.EMPTY_FILE,
        "file is empty",
        details={"size": 0},
    )
    assert error.to_dict() == {
        "code": "EMPTY_FILE",
        "message": "file is empty",
        "details": {"size": 0},
    }


def test_utc_now_is_timezone_aware():
    assert utc_now().tzinfo is not None

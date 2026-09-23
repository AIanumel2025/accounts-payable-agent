"""Unit tests for Phase 1 artifact behaviour: `preserve_original_document`
and `build_document_record` from `ap_agent.tools.ingestion` (notebook
cell 14).

Phase 1 has no JSON serialiser or generic atomic-write helper of its own
(`docs/modularisation_map.md` §2 lists no Phase 1 entry for
`artifacts/filesystem.py` or `artifacts/serialization.py`; the earliest
entries there are cell 26, Phase 2). Its only filesystem behaviour is
content-addressed, byte-for-byte original preservation, implemented
entirely inside `preserve_original_document`. This file exercises that
behaviour directly, using `tmp_path` for every write.
"""

from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.config.settings import IngestionConfig
from ap_agent.exceptions import IngestionValidationError
from ap_agent.models.ingestion import DocumentIdentity, IngestionErrorCode
from ap_agent.tools.ingestion import (
    build_document_identity,
    build_document_record,
    calculate_sha256,
    preserve_original_document,
    validate_intake,
)

PDF_BYTES = b"%PDF-1.4\nExample Accounts Payable Invoice INV-1001\n%%EOF"


def ingest_bytes(tmp_path: Path, name: str, content: bytes):
    """Validate + fingerprint + identify one synthetic document."""
    incoming = tmp_path / "incoming"
    incoming.mkdir(exist_ok=True)
    path = incoming / name
    path.write_bytes(content)

    config = IngestionConfig(artifact_root=tmp_path / "artifacts")
    inspection = validate_intake(path, config)
    sha256 = calculate_sha256(path)
    identity = build_document_identity(batch_id=uuid4(), sha256=sha256)
    return path, inspection, identity, config


# --- byte preservation ---------------------------------------------------


def test_preserve_original_document_is_byte_for_byte(tmp_path):
    path, inspection, identity, config = ingest_bytes(
        tmp_path, "invoice.pdf", PDF_BYTES
    )

    stored_path, created = preserve_original_document(
        source_path=path, identity=identity, inspection=inspection, config=config
    )

    assert created is True
    assert stored_path.read_bytes() == PDF_BYTES


def test_preserved_artifact_sha256_matches_the_registered_identity(tmp_path):
    path, inspection, identity, config = ingest_bytes(
        tmp_path, "invoice.pdf", PDF_BYTES
    )

    stored_path, _ = preserve_original_document(
        source_path=path, identity=identity, inspection=inspection, config=config
    )

    assert calculate_sha256(stored_path) == identity.sha256


def test_artifact_layout_is_content_addressed(tmp_path):
    path, inspection, identity, config = ingest_bytes(
        tmp_path, "invoice.pdf", PDF_BYTES
    )

    stored_path, _ = preserve_original_document(
        source_path=path, identity=identity, inspection=inspection, config=config
    )

    expected_path = (
        config.artifact_root / "originals" / str(identity.content_id) / "original.pdf"
    ).resolve()
    assert stored_path == expected_path


def test_no_writes_happen_outside_the_configured_artifact_root(tmp_path):
    path, inspection, identity, config = ingest_bytes(
        tmp_path, "invoice.pdf", PDF_BYTES
    )

    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    preserve_original_document(
        source_path=path, identity=identity, inspection=inspection, config=config
    )
    after = {p for p in tmp_path.rglob("*") if p.is_file()}

    new_files = after - before
    assert new_files, "expected at least one new artifact file"
    for new_file in new_files:
        assert config.artifact_root in new_file.parents


# --- idempotency and duplicate policy ------------------------------------


def test_repeated_preservation_is_idempotent(tmp_path):
    path, inspection, identity, config = ingest_bytes(
        tmp_path, "invoice.pdf", PDF_BYTES
    )

    first_path, first_created = preserve_original_document(
        source_path=path, identity=identity, inspection=inspection, config=config
    )
    second_path, second_created = preserve_original_document(
        source_path=path, identity=identity, inspection=inspection, config=config
    )

    assert first_created is True
    assert second_created is False
    assert first_path == second_path
    assert first_path.read_bytes() == PDF_BYTES


def test_renamed_byte_identical_copy_resolves_to_the_same_artifact(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    original_path = incoming / "invoice_1001.pdf"
    renamed_path = incoming / "renamed_invoice.pdf"
    original_path.write_bytes(PDF_BYTES)
    renamed_path.write_bytes(PDF_BYTES)

    config = IngestionConfig(artifact_root=tmp_path / "artifacts")
    batch_id = uuid4()

    original_inspection = validate_intake(original_path, config)
    original_identity = build_document_identity(
        batch_id=batch_id, sha256=calculate_sha256(original_path)
    )
    renamed_inspection = validate_intake(renamed_path, config)
    renamed_identity = build_document_identity(
        batch_id=batch_id, sha256=calculate_sha256(renamed_path)
    )

    # Same batch, same bytes: identical content and document identity,
    # regardless of the two different filenames.
    assert original_identity.content_id == renamed_identity.content_id
    assert original_identity.document_id == renamed_identity.document_id

    original_stored, original_created = preserve_original_document(
        source_path=original_path,
        identity=original_identity,
        inspection=original_inspection,
        config=config,
    )
    renamed_stored, renamed_created = preserve_original_document(
        source_path=renamed_path,
        identity=renamed_identity,
        inspection=renamed_inspection,
        config=config,
    )

    assert original_created is True
    assert renamed_created is False
    assert original_stored == renamed_stored


def test_same_filename_different_bytes_do_not_collide(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    first_path = incoming / "invoice.pdf"
    first_path.write_bytes(PDF_BYTES)

    config = IngestionConfig(artifact_root=tmp_path / "artifacts")
    batch_id = uuid4()

    first_inspection = validate_intake(first_path, config)
    first_identity = build_document_identity(
        batch_id=batch_id, sha256=calculate_sha256(first_path)
    )
    first_stored, _ = preserve_original_document(
        source_path=first_path,
        identity=first_identity,
        inspection=first_inspection,
        config=config,
    )

    # Overwrite the same filename with different bytes and re-run ingestion
    # against the *new* content: this must be treated as a different
    # document, stored at a different, content-addressed path.
    different_content = PDF_BYTES + b"\nEXTRA PAGE"
    first_path.write_bytes(different_content)

    second_inspection = validate_intake(first_path, config)
    second_identity = build_document_identity(
        batch_id=batch_id, sha256=calculate_sha256(first_path)
    )
    second_stored, second_created = preserve_original_document(
        source_path=first_path,
        identity=second_identity,
        inspection=second_inspection,
        config=config,
    )

    assert first_identity.content_id != second_identity.content_id
    assert second_created is True
    assert second_stored != first_stored
    assert second_stored.read_bytes() == different_content
    # The first artifact is untouched by the second, different-content write.
    assert first_stored.read_bytes() == PDF_BYTES


def test_artifact_collision_with_different_bytes_raises_hash_mismatch(tmp_path):
    path, inspection, identity, config = ingest_bytes(
        tmp_path, "invoice.pdf", PDF_BYTES
    )

    # Simulate a corrupted/tampered artifact already sitting at the
    # content-addressed destination path, with the wrong bytes for its
    # claimed content_id.
    destination_directory = config.artifact_root / "originals" / str(identity.content_id)
    destination_directory.mkdir(parents=True)
    (destination_directory / "original.pdf").write_bytes(b"corrupted bytes")

    with pytest.raises(IngestionValidationError) as excinfo:
        preserve_original_document(
            source_path=path, identity=identity, inspection=inspection, config=config
        )

    assert excinfo.value.code == IngestionErrorCode.HASH_MISMATCH


def test_storage_failure_cleans_up_its_temporary_file_and_fails_closed(
    tmp_path, monkeypatch
):
    path, inspection, identity, config = ingest_bytes(
        tmp_path, "invoice.pdf", PDF_BYTES
    )

    def broken_copyfileobj(*_args, **_kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(
        "ap_agent.tools.ingestion.shutil.copyfileobj", broken_copyfileobj
    )

    with pytest.raises(IngestionValidationError) as excinfo:
        preserve_original_document(
            source_path=path, identity=identity, inspection=inspection, config=config
        )

    assert excinfo.value.code == IngestionErrorCode.STORAGE_FAILED

    destination_directory = config.artifact_root / "originals" / str(identity.content_id)
    leftover_files = list(destination_directory.iterdir())
    assert leftover_files == [], f"temporary file was not cleaned up: {leftover_files}"


# --- build_document_record ------------------------------------------------


def test_build_document_record_carries_identity_and_storage_metadata(tmp_path):
    path, inspection, identity, config = ingest_bytes(
        tmp_path, "invoice.pdf", PDF_BYTES
    )
    stored_path, _ = preserve_original_document(
        source_path=path, identity=identity, inspection=inspection, config=config
    )

    document = build_document_record(
        inspection=inspection,
        identity=identity,
        stored_path=stored_path,
        ingestion_source="unit-test",
    )

    assert document.document_id == identity.document_id
    assert document.batch_id == identity.batch_id
    assert document.sha256 == identity.sha256
    assert document.original_filename == "invoice.pdf"
    assert document.metadata["content_id"] == str(identity.content_id)
    assert document.metadata["stored_original_path"] == str(stored_path)
    assert document.metadata["file_size_bytes"] == inspection.file_size_bytes
    assert document.metadata["ingestion_source"] == "unit-test"


def test_build_document_record_starts_pending_for_downstream_stages(tmp_path):
    from ap_agent.models.common import ProcessingStatus

    path, inspection, identity, config = ingest_bytes(
        tmp_path, "invoice.pdf", PDF_BYTES
    )
    stored_path, _ = preserve_original_document(
        source_path=path, identity=identity, inspection=inspection, config=config
    )

    document = build_document_record(
        inspection=inspection,
        identity=identity,
        stored_path=stored_path,
        ingestion_source="unit-test",
    )

    assert document.status == ProcessingStatus.PENDING


def test_document_identity_is_a_frozen_contract():
    identity = DocumentIdentity(
        batch_id=uuid4(), document_id=uuid4(), content_id=uuid4(), sha256="0" * 64
    )
    with pytest.raises(Exception):
        identity.sha256 = "1" * 64

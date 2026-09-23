"""Unit tests for the Phase 1 processing functions in
`ap_agent.tools.ingestion` (notebook cells 12 and 14).

These exercise each function in isolation with synthetic bytes created in
`tmp_path`. No committed invoice fixture is read here (that is covered by
`tests/integration/test_phase_1_ingestion.py`); per CLAUDE.md and decisions
D-2/D-3, nothing here hardcodes a fixture filename, hash or expected total.
"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from ap_agent.config.settings import IngestionConfig
from ap_agent.exceptions import IngestionValidationError
from ap_agent.models.ingestion import IngestionErrorCode
from ap_agent.tools.ingestion import (
    BATCH_NAMESPACE,
    CONTENT_NAMESPACE,
    DOCUMENT_NAMESPACE,
    build_document_identity,
    calculate_sha256,
    create_batch_id,
    detect_document_media_type,
    is_exact_duplicate,
    validate_intake,
    validate_sha256,
)

PDF_BYTES = b"%PDF-1.4\nExample Accounts Payable Invoice INV-1001\n%%EOF"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n" + b"rest-of-file"
JPEG_SIGNATURE = b"\xff\xd8\xff" + b"rest-of-file"
TIFF_LE_SIGNATURE = b"II*\x00" + b"rest-of-file"
TIFF_BE_SIGNATURE = b"MM\x00*" + b"rest-of-file"


def make_config(tmp_path: Path, **overrides) -> IngestionConfig:
    return IngestionConfig(artifact_root=tmp_path / "artifacts", **overrides)


# --- create_batch_id -------------------------------------------------


def test_create_batch_id_is_deterministic_with_an_external_reference():
    first = create_batch_id("local_notebook_upload", "phase-1-ingestion-test")
    second = create_batch_id("local_notebook_upload", "phase-1-ingestion-test")
    assert first == second
    assert isinstance(first, UUID)


def test_create_batch_id_matches_the_uuid5_formula():
    from uuid import uuid5

    batch_id = create_batch_id("Local_Upload ", " Phase-1-Test ")
    expected = uuid5(BATCH_NAMESPACE, "local_upload:phase-1-test")
    assert batch_id == expected


def test_create_batch_id_normalizes_case_and_whitespace_before_hashing():
    first = create_batch_id("Local_Upload", "Phase-1-Test")
    second = create_batch_id("  local_upload  ", "  phase-1-test  ")
    assert first == second


def test_create_batch_id_without_a_reference_is_random_each_call():
    first = create_batch_id("local_notebook_upload")
    second = create_batch_id("local_notebook_upload")
    assert first != second


def test_create_batch_id_rejects_empty_source():
    with pytest.raises(ValueError):
        create_batch_id("   ")


def test_create_batch_id_rejects_blank_external_reference():
    with pytest.raises(ValueError):
        create_batch_id("local_notebook_upload", "   ")


def test_create_batch_id_does_not_depend_on_a_timestamp():
    # Same inputs called at two distinct points in time must still collide.
    import time

    first = create_batch_id("local_notebook_upload", "phase-1-ingestion-test")
    time.sleep(0.01)
    second = create_batch_id("local_notebook_upload", "phase-1-ingestion-test")
    assert first == second


# --- detect_document_media_type --------------------------------------


@pytest.mark.parametrize(
    ("content", "expected_media_type"),
    [
        (PDF_BYTES, "application/pdf"),
        (PNG_SIGNATURE, "image/png"),
        (JPEG_SIGNATURE, "image/jpeg"),
        (TIFF_LE_SIGNATURE, "image/tiff"),
        (TIFF_BE_SIGNATURE, "image/tiff"),
    ],
)
def test_detect_document_media_type_sniffs_supported_signatures(
    tmp_path, content, expected_media_type
):
    path = tmp_path / "document.bin"
    path.write_bytes(content)
    assert detect_document_media_type(path) == expected_media_type


def test_detect_document_media_type_rejects_unrecognized_signature(tmp_path):
    path = tmp_path / "document.bin"
    path.write_bytes(b"not a real document signature at all")

    with pytest.raises(IngestionValidationError) as excinfo:
        detect_document_media_type(path)

    assert excinfo.value.code == IngestionErrorCode.UNSUPPORTED_CONTENT


# --- validate_intake ---------------------------------------------------


def test_validate_intake_accepts_a_well_formed_pdf(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(PDF_BYTES)
    config = make_config(tmp_path)

    inspection = validate_intake(path, config)

    assert inspection.original_filename == "invoice.pdf"
    assert inspection.extension == ".pdf"
    assert inspection.media_type == "application/pdf"
    assert inspection.file_size_bytes == len(PDF_BYTES)
    assert inspection.source_path == path.resolve()


def test_validate_intake_raises_file_not_found(tmp_path):
    config = make_config(tmp_path)

    with pytest.raises(IngestionValidationError) as excinfo:
        validate_intake(tmp_path / "missing.pdf", config)

    assert excinfo.value.code == IngestionErrorCode.FILE_NOT_FOUND


def test_validate_intake_raises_not_a_file_for_a_directory(tmp_path):
    directory = tmp_path / "a_directory.pdf"
    directory.mkdir()
    config = make_config(tmp_path)

    with pytest.raises(IngestionValidationError) as excinfo:
        validate_intake(directory, config)

    assert excinfo.value.code == IngestionErrorCode.NOT_A_FILE


def test_validate_intake_raises_empty_file(tmp_path):
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")
    config = make_config(tmp_path)

    with pytest.raises(IngestionValidationError) as excinfo:
        validate_intake(path, config)

    assert excinfo.value.code == IngestionErrorCode.EMPTY_FILE


def test_validate_intake_raises_file_too_large(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(PDF_BYTES)
    config = make_config(tmp_path, maximum_file_size_bytes=len(PDF_BYTES) - 1)

    with pytest.raises(IngestionValidationError) as excinfo:
        validate_intake(path, config)

    assert excinfo.value.code == IngestionErrorCode.FILE_TOO_LARGE


def test_validate_intake_raises_unsupported_extension(tmp_path):
    path = tmp_path / "invoice.txt"
    path.write_bytes(b"Unsupported invoice file.")
    config = make_config(tmp_path)

    with pytest.raises(IngestionValidationError) as excinfo:
        validate_intake(path, config)

    assert excinfo.value.code == IngestionErrorCode.UNSUPPORTED_EXTENSION


def test_validate_intake_raises_unsupported_content_for_a_fake_pdf(tmp_path):
    path = tmp_path / "fake_invoice.pdf"
    path.write_bytes(b"This is not really a PDF.")
    config = make_config(tmp_path)

    with pytest.raises(IngestionValidationError) as excinfo:
        validate_intake(path, config)

    assert excinfo.value.code == IngestionErrorCode.UNSUPPORTED_CONTENT


def test_validate_intake_raises_extension_content_mismatch(tmp_path):
    # A real PNG signature saved with a .pdf extension.
    path = tmp_path / "invoice.pdf"
    path.write_bytes(PNG_SIGNATURE)
    config = make_config(tmp_path)

    with pytest.raises(IngestionValidationError) as excinfo:
        validate_intake(path, config)

    assert excinfo.value.code == IngestionErrorCode.EXTENSION_CONTENT_MISMATCH


# --- calculate_sha256 / validate_sha256 --------------------------------


def test_calculate_sha256_matches_hashlib(tmp_path):
    import hashlib

    path = tmp_path / "invoice.pdf"
    path.write_bytes(PDF_BYTES)

    assert calculate_sha256(path) == hashlib.sha256(PDF_BYTES).hexdigest()


def test_calculate_sha256_is_correct_across_multiple_chunks(tmp_path):
    import hashlib

    content = b"x" * 10_000
    path = tmp_path / "large.pdf"
    path.write_bytes(content)

    assert calculate_sha256(path, chunk_size=64) == hashlib.sha256(content).hexdigest()


def test_calculate_sha256_rejects_non_positive_chunk_size(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(PDF_BYTES)

    with pytest.raises(ValueError):
        calculate_sha256(path, chunk_size=0)


def test_validate_sha256_normalizes_case_and_whitespace():
    normalized = validate_sha256("  " + ("AB" * 32) + "  ")
    assert normalized == ("ab" * 32)


def test_validate_sha256_rejects_wrong_length():
    with pytest.raises(ValueError):
        validate_sha256("abc123")


def test_validate_sha256_rejects_non_hexadecimal_characters():
    with pytest.raises(ValueError):
        validate_sha256("z" * 64)


# --- build_document_identity (deterministic ID formulas) ---------------


def test_build_document_identity_is_deterministic():
    batch_id = uuid4()
    sha256 = "a" * 64

    first = build_document_identity(batch_id=batch_id, sha256=sha256)
    second = build_document_identity(batch_id=batch_id, sha256=sha256)

    assert first.content_id == second.content_id
    assert first.document_id == second.document_id


def test_build_document_identity_matches_the_uuid5_formulas():
    from uuid import uuid5

    batch_id = uuid4()
    sha256 = "b" * 64

    identity = build_document_identity(batch_id=batch_id, sha256=sha256)

    assert identity.content_id == uuid5(CONTENT_NAMESPACE, sha256)
    assert identity.document_id == uuid5(
        DOCUMENT_NAMESPACE, f"{batch_id}:{sha256}"
    )


def test_changing_batch_id_only_affects_the_document_id():
    sha256 = "c" * 64
    first_batch, second_batch = uuid4(), uuid4()

    first_identity = build_document_identity(batch_id=first_batch, sha256=sha256)
    second_identity = build_document_identity(batch_id=second_batch, sha256=sha256)

    # content_id depends only on the byte content, never on the batch.
    assert first_identity.content_id == second_identity.content_id
    # document_id is scoped to the batch, so it must differ.
    assert first_identity.document_id != second_identity.document_id


def test_different_content_produces_different_identities():
    batch_id = uuid4()

    first_identity = build_document_identity(batch_id=batch_id, sha256="d" * 64)
    second_identity = build_document_identity(batch_id=batch_id, sha256="e" * 64)

    assert first_identity.content_id != second_identity.content_id
    assert first_identity.document_id != second_identity.document_id


def test_build_document_identity_normalizes_the_stored_hash_case():
    batch_id = uuid4()
    identity = build_document_identity(batch_id=batch_id, sha256="F" * 64)
    assert identity.sha256 == "f" * 64


def test_deterministic_ids_match_the_modularisation_map_synthetic_baseline():
    """Cross-checks against docs/modularisation_map.md §1.4, which
    independently re-derived these values with uuid5/hashlib against the
    notebook's cell-16 synthetic byte strings (not sourced from this
    codebase)."""
    import hashlib

    test_batch_id = create_batch_id("local_notebook_upload", "phase-1-ingestion-test")
    assert str(test_batch_id) == "2e982b5f-16e5-525b-9f05-aa3bef9e78aa"

    first_content = (
        b"%PDF-1.4\n"
        b"Example Accounts Payable Invoice INV-1001\n"
        b"%%EOF"
    )
    different_content = (
        b"%PDF-1.4\n"
        b"Example Accounts Payable Invoice INV-1002\n"
        b"%%EOF"
    )

    first_identity = build_document_identity(
        batch_id=test_batch_id, sha256=hashlib.sha256(first_content).hexdigest()
    )
    different_identity = build_document_identity(
        batch_id=test_batch_id, sha256=hashlib.sha256(different_content).hexdigest()
    )

    assert str(first_identity.document_id) == "65edbfaf-e250-5163-bda3-a5b4e0220465"
    assert str(first_identity.content_id) == "71f61c11-f36f-54b4-8357-d45c55d661a6"
    assert str(different_identity.document_id) == "8af7823a-6b04-5469-b9f5-0849c21fd95c"


# --- is_exact_duplicate -------------------------------------------------


def test_is_exact_duplicate_true_for_known_hash():
    sha256 = "a" * 64
    assert is_exact_duplicate(sha256, {sha256}) is True


def test_is_exact_duplicate_false_for_unknown_hash():
    assert is_exact_duplicate("a" * 64, {"b" * 64}) is False


def test_is_exact_duplicate_normalizes_case_on_both_sides():
    assert is_exact_duplicate("A" * 64, {"a" * 64}) is True


def test_is_exact_duplicate_with_empty_known_set():
    assert is_exact_duplicate("a" * 64, set()) is False


# --- import hygiene (mirrors test_package_foundation.py's M2 checks) ----


def test_tools_ingestion_module_imports_successfully():
    import importlib

    module = importlib.import_module("ap_agent.tools.ingestion")
    assert module is not None


def test_tools_ingestion_module_does_not_import_heavy_optional_dependencies():
    import importlib
    import sys

    forbidden = {
        "pandas",
        "matplotlib",
        "IPython",
        "paddle",
        "paddleocr",
        "pytesseract",
        "pymupdf",
        "cv2",
        "numpy",
        "PIL",
    }

    before = set(sys.modules)
    importlib.import_module("ap_agent.tools.ingestion")
    after = set(sys.modules)

    leaked = {
        name for name in (after - before) if name.split(".")[0] in forbidden
    }
    assert not leaked, f"ap_agent.tools.ingestion pulled in heavy dependencies: {leaked}"


def test_tools_ingestion_module_does_not_reference_content_path():
    source = Path(
        Path(__file__).resolve().parents[2]
        / "src"
        / "ap_agent"
        / "tools"
        / "ingestion.py"
    ).read_text(encoding="utf-8")
    assert "/content" not in source

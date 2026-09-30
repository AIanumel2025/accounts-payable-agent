"""M11D Core upload validation and local artifact storage (no database)."""

from __future__ import annotations

import io
import os
import uuid

import pytest

from ap_agent.artifacts.storage import LocalFilesystemArtifactStore, parse_artifact_uri
from ap_agent.artifacts.upload_validation import (
    detect_media_type,
    reject_forbidden_form_fields,
    validate_upload_content,
    validate_upload_filename,
)
from ap_agent.exceptions import ArtifactIntegrityError, OperationsRequestRejectedError
from ap_agent.models.operations import UploadLimits

pytestmark = pytest.mark.unit

LIMITS = UploadLimits()
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 17 + b"IEND\xaeB`\x82"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 10 + b"\xff\xd9"


def _code(callable_, *args, **kwargs) -> str:
    with pytest.raises(OperationsRequestRejectedError) as raised:
        callable_(*args, **kwargs)
    return raised.value.code


@pytest.mark.parametrize("name", ["../x.pdf", "a/b.pdf", "a\\b.pdf", "..pdf", ".hidden.pdf", " lead.pdf", "x\x00.pdf", "x\n.pdf"])
def test_path_like_or_control_filenames_are_refused(name):
    assert _code(validate_upload_filename, name, LIMITS) == "FILENAME_INVALID"


def test_filename_limits_and_types():
    assert _code(validate_upload_filename, None, LIMITS) == "FILENAME_MISSING"
    assert _code(validate_upload_filename, "a" * 129 + ".pdf", LIMITS) == "FILENAME_TOO_LONG"
    assert _code(validate_upload_filename, "invoice.exe", LIMITS) == "UNSUPPORTED_FILE_TYPE"
    assert _code(validate_upload_filename, "noextension", LIMITS) == "UNSUPPORTED_FILE_TYPE"

    name = validate_upload_filename("Invoice (March) [v2].JPG", LIMITS)
    assert (name.extension, name.media_type) == (".jpg", "image/jpeg")


@pytest.mark.parametrize(
    ("filename", "content", "declared"),
    [("a.pdf", PDF, "application/pdf"), ("a.png", PNG, "image/png"), ("a.jpeg", JPEG, "image/jpeg"),
     ("a.pdf", PDF, None), ("a.png", PNG, "image/png; charset=binary")],
)
def test_consistent_uploads_are_accepted(filename, content, declared):
    name = validate_upload_filename(filename, LIMITS)
    validate_upload_content(name=name, declared_media_type=declared, head=content[:1024], tail=content[-1024:],
                            size=len(content), limits=LIMITS)


@pytest.mark.parametrize(
    ("filename", "content", "declared", "code"),
    [
        ("a.pdf", b"", "application/pdf", "EMPTY_FILE"),
        ("a.pdf", PNG, "application/pdf", "FILE_SIGNATURE_MISMATCH"),
        ("a.png", PDF, "image/png", "FILE_SIGNATURE_MISMATCH"),
        ("a.pdf", PDF, "image/png", "MEDIA_TYPE_MISMATCH"),
        ("a.pdf", b"not a pdf", "application/pdf", "UNSUPPORTED_FILE_TYPE"),
        ("a.pdf", b"%PDF-1.4 cut short", "application/pdf", "MALFORMED_FILE"),
        ("a.png", PNG[:-4], "image/png", "MALFORMED_FILE"),
        ("a.jpg", JPEG[:-2], "image/jpeg", "MALFORMED_FILE"),
    ],
)
def test_inconsistent_or_malformed_uploads_are_refused(filename, content, declared, code):
    name = validate_upload_filename(filename, LIMITS)
    assert _code(validate_upload_content, name=name, declared_media_type=declared, head=content[:1024],
                 tail=content[-1024:], size=len(content), limits=LIMITS) == code


def test_oversized_content_is_refused_by_size():
    name = validate_upload_filename("a.pdf", UploadLimits(maximum_file_bytes=10, maximum_request_bytes=100))
    assert _code(validate_upload_content, name=name, declared_media_type=None, head=PDF, tail=PDF, size=11,
                 limits=UploadLimits(maximum_file_bytes=10, maximum_request_bytes=100)) == "FILE_TOO_LARGE"


def test_detect_media_type():
    assert [detect_media_type(x) for x in (PDF, PNG, JPEG, b"GIF89a")] == [
        "application/pdf", "image/png", "image/jpeg", None]


@pytest.mark.parametrize("field", ["payment_amount", "bank_account", "ERP_post", "execute_payment", "iban", "wire_to", "post_to_erp"])
def test_payment_shaped_fields_get_their_own_code(field):
    assert _code(reject_forbidden_form_fields, ["file", field]) == "PAYMENT_FIELD_PROHIBITED"


@pytest.mark.parametrize("field", ["tenant_id", "actor", "role", "note"])
def test_any_other_field_is_refused(field):
    assert _code(reject_forbidden_form_fields, [field]) == "FIELD_NOT_ALLOWED"
    reject_forbidden_form_fields(["file"])


# -- storage ---------------------------------------------------------------


def _store(tmp_path):
    return LocalFilesystemArtifactStore(tmp_path / "root")


def test_stage_commit_verify_round_trip_with_hash_and_size(tmp_path):
    import hashlib

    store = _store(tmp_path)
    tenant, job = uuid.uuid4(), uuid.uuid4()

    staged = store.stage(tenant, io.BytesIO(PDF), maximum_bytes=1024)
    assert staged.sha256 == hashlib.sha256(PDF).hexdigest() and staged.size == len(PDF)

    uri = store.commit(staged, tenant_id=tenant, job_id=job, name="invoice.pdf")
    assert uri == f"artifact://{tenant.hex}/{job.hex}/invoice.pdf"
    assert not staged.staging_path.exists()

    path = store.open_verified(uri, tenant_id=tenant, expected_sha256=staged.sha256)
    assert path.read_bytes() == PDF and str(tmp_path / "root") in str(path)


def test_oversized_stream_is_cut_off_and_leaves_no_partial_file(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(OperationsRequestRejectedError) as raised:
        store.stage(uuid.uuid4(), io.BytesIO(b"x" * 5000), maximum_bytes=1000)

    assert raised.value.code == "FILE_TOO_LARGE"
    assert [p for p in (tmp_path / "root").rglob("*") if p.is_file()] == []


def test_tampering_a_wrong_tenant_and_a_missing_file_fail_closed(tmp_path):
    store = _store(tmp_path)
    tenant, job = uuid.uuid4(), uuid.uuid4()
    staged = store.stage(tenant, io.BytesIO(PDF), maximum_bytes=1024)
    uri = store.commit(staged, tenant_id=tenant, job_id=job, name="a.pdf")

    with pytest.raises(ArtifactIntegrityError, match="ARTIFACT_TENANT_MISMATCH"):
        store.open_verified(uri, tenant_id=uuid.uuid4(), expected_sha256=staged.sha256)

    with pytest.raises(ArtifactIntegrityError, match="ARTIFACT_HASH_MISMATCH"):
        store.open_verified(uri, tenant_id=tenant, expected_sha256="0" * 64)

    store.delete(uri)

    with pytest.raises(ArtifactIntegrityError, match="ARTIFACT_MISSING"):
        store.open_verified(uri, tenant_id=tenant, expected_sha256=staged.sha256)


@pytest.mark.parametrize(
    "uri",
    [
        "artifact://../../etc/passwd", "artifact://" + "a" * 32 + "/" + "b" * 32 + "/../../x.pdf",
        "artifact://" + "a" * 32 + "/" + "b" * 32 + "/a/b.pdf", "file:///etc/passwd", "/etc/passwd",
        "artifact://" + "A" * 32 + "/" + "b" * 32 + "/x.pdf",
    ],
)
def test_malformed_or_traversing_references_are_rejected(uri):
    with pytest.raises(ArtifactIntegrityError):
        parse_artifact_uri(uri)


def test_a_symlinked_directory_cannot_escape_the_root(tmp_path):
    store = _store(tmp_path)
    tenant, job = uuid.uuid4(), uuid.uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.pdf").write_bytes(PDF)

    tenant_directory = tmp_path / "root" / tenant.hex
    tenant_directory.mkdir(parents=True)
    os.symlink(outside, tenant_directory / job.hex)

    with pytest.raises(ArtifactIntegrityError, match="ARTIFACT_PATH_ESCAPE"):
        store.open_verified(f"artifact://{tenant.hex}/{job.hex}/a.pdf", tenant_id=tenant, expected_sha256="0" * 64)


def test_tenants_get_separate_directories(tmp_path):
    store = _store(tmp_path)
    a, b = uuid.uuid4(), uuid.uuid4()
    uri_a = store.commit(store.stage(a, io.BytesIO(PDF), maximum_bytes=1024), tenant_id=a, job_id=uuid.uuid4(), name="x.pdf")
    uri_b = store.commit(store.stage(b, io.BytesIO(PDF), maximum_bytes=1024), tenant_id=b, job_id=uuid.uuid4(), name="x.pdf")

    assert a.hex in uri_a and b.hex in uri_b and a.hex not in uri_b

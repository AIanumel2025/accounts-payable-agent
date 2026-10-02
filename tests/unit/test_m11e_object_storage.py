"""M11E: S3-compatible artifact store contract (injected fake client)."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tempfile
import uuid
from pathlib import Path

import pytest

from ap_agent.artifacts.s3 import S3ArtifactStore, object_key_for, parse_object_uri
from ap_agent.exceptions import ArtifactIntegrityError, OperationsRequestRejectedError
from tests.support.fake_s3 import FakeS3Client

pytestmark = pytest.mark.unit

BUCKET = "example-bucket"
PAYLOAD = b"%PDF-1.7\n" + b"0123456789" * 500 + b"\n%%EOF\n"
SHA = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture()
def staging(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "scratch"
    directory.mkdir()
    monkeypatch.setenv("TMPDIR", str(directory))
    monkeypatch.setattr(tempfile, "tempdir", None)
    return directory


@pytest.fixture()
def client() -> FakeS3Client:
    return FakeS3Client()


@pytest.fixture()
def store(client, staging) -> S3ArtifactStore:
    return S3ArtifactStore(client, BUCKET, staging_directory=staging)


def _commit(store, tenant=None, job=None, name="Invoice 1.pdf", payload=PAYLOAD):
    tenant = tenant or uuid.uuid4()
    job = job or uuid.uuid4()
    staged = store.stage(tenant, io.BytesIO(payload), maximum_bytes=1_000_000)
    return tenant, job, store.commit(staged, tenant_id=tenant, job_id=job, name=name)


def test_uploads_are_keyed_by_tenant_job_and_hash_never_by_filename(store, client):
    tenant, job, uri = _commit(store, name="../../etc/passwd invoice.pdf".replace("/", "_"))

    assert uri == f"object://{tenant.hex}/{job.hex}/{SHA}"
    [(bucket, key)] = client.objects
    assert bucket == BUCKET
    assert key == f"tenants/{tenant.hex}/jobs/{job.hex}/source/{SHA}"
    assert "passwd" not in key and ".pdf" not in key


def test_size_content_type_and_hash_are_stored_with_the_object(store, client):
    _commit(store)
    [stored] = client.objects.values()
    assert stored["ContentType"] == "application/pdf"
    assert stored["Metadata"] == {"sha256": SHA, "size": str(len(PAYLOAD))}
    assert stored["Params"]["ContentLength"] == len(PAYLOAD)
    assert stored["Params"]["ChecksumSHA256"]


def test_no_public_access_or_url_is_ever_requested(store, client):
    _commit(store)
    [(name, params)] = [call for call in client.calls if call[0] == "put_object"]
    assert not {"ACL", "GrantRead", "WebsiteRedirectLocation"} & set(params)
    source = Path(__import__("ap_agent.artifacts.s3", fromlist=["x"]).__file__).read_text()
    assert "presign" not in source.lower() and "public-read" not in source


def test_staging_file_is_removed_after_commit(store, staging):
    _commit(store)
    assert list(staging.iterdir()) == []


def test_recommitting_the_same_bytes_for_the_same_job_is_idempotent(store, client):
    tenant, job, first = _commit(store)
    staged = store.stage(tenant, io.BytesIO(PAYLOAD), maximum_bytes=1_000_000)
    assert store.commit(staged, tenant_id=tenant, job_id=job, name="Invoice 1.pdf") == first
    assert client.count("put_object") == 1


@pytest.mark.parametrize(
    "uri",
    [
        f"object://{'a' * 32}/{'b' * 32}/../{'c' * 64}",
        f"object://{'a' * 32}/{'b' * 32}/{'c' * 63}",
        f"object://{'A' * 32}/{'b' * 32}/{'c' * 64}",
        f"object://{'a' * 32}/..%2f{'b' * 29}/{'c' * 64}",
        f"object://{'a' * 32}/{'b' * 32}/{'c' * 64}/extra",
        f"object://{'a' * 32}/{'b' * 32}/{'c' * 64}?x=1",
        "artifact://a/b/c.pdf",
        "object://../../x",
        "",
    ],
)
def test_malformed_or_traversing_references_are_refused(uri):
    with pytest.raises(ArtifactIntegrityError) as caught:
        parse_object_uri(uri)

    assert caught.value.code == "ARTIFACT_REFERENCE_INVALID"


def test_keys_can_only_be_built_from_validated_components():
    tenant, job, sha = parse_object_uri(f"object://{'a' * 32}/{'b' * 32}/{'c' * 64}")
    key = object_key_for(tenant, job, sha)
    assert key.count("/") == 5 and ".." not in key


def test_another_tenants_reference_is_refused_before_any_storage_request(store, client):
    tenant, job, uri = _commit(store)
    client.calls.clear()

    with pytest.raises(ArtifactIntegrityError) as caught:
        with store.verified_local_copy(uri, tenant_id=uuid.uuid4(), expected_sha256=SHA, filename="a.pdf"):
            pass

    assert caught.value.code == "ARTIFACT_TENANT_MISMATCH"
    assert client.calls == []


def test_recorded_hash_must_match_the_reference(store):
    tenant, job, uri = _commit(store)

    with pytest.raises(ArtifactIntegrityError) as caught:
        with store.verified_local_copy(uri, tenant_id=tenant, expected_sha256="0" * 64, filename="a.pdf"):
            pass

    assert caught.value.code == "ARTIFACT_HASH_MISMATCH"


def test_worker_copy_is_private_verified_and_removed_after_success(store, staging):
    tenant, job, uri = _commit(store)

    with store.verified_local_copy(uri, tenant_id=tenant, expected_sha256=SHA, filename="Invoice 1.pdf", expected_size=len(PAYLOAD)) as path:
        assert path.read_bytes() == PAYLOAD
        assert path.name == "Invoice 1.pdf"
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
        assert path.parent.parent == staging

    assert not path.exists() and not path.parent.exists()
    assert list(staging.iterdir()) == []


def test_worker_copy_is_removed_when_processing_fails(store, staging):
    tenant, job, uri = _commit(store)

    with pytest.raises(RuntimeError):
        with store.verified_local_copy(uri, tenant_id=tenant, expected_sha256=SHA, filename="a.pdf"):
            raise RuntimeError("pipeline blew up")

    assert list(staging.iterdir()) == []


@pytest.mark.parametrize(
    ("attribute", "code"),
    [("corrupt_get", "ARTIFACT_HASH_MISMATCH"), ("truncate_get", "ARTIFACT_SIZE_MISMATCH"), ("lie_about_metadata", "ARTIFACT_METADATA_MISMATCH")],
)
def test_tampered_objects_are_rejected_and_nothing_is_left_behind(store, client, staging, attribute, code):
    tenant, job, uri = _commit(store)
    setattr(client, attribute, True)

    with pytest.raises(ArtifactIntegrityError) as caught:
        with store.verified_local_copy(uri, tenant_id=tenant, expected_sha256=SHA, filename="a.pdf"):
            pytest.fail("a corrupt object must never be handed to the pipeline")

    assert caught.value.code == code
    assert list(staging.iterdir()) == []


def test_wrong_expected_size_is_rejected(store):
    tenant, job, uri = _commit(store)

    with pytest.raises(ArtifactIntegrityError) as caught:
        with store.verified_local_copy(uri, tenant_id=tenant, expected_sha256=SHA, filename="a.pdf", expected_size=1):
            pass

    assert caught.value.code == "ARTIFACT_SIZE_MISMATCH"


def test_missing_object_is_reported(store, client):
    tenant, job, uri = _commit(store)
    client.objects.clear()

    with pytest.raises(ArtifactIntegrityError) as caught:
        store.verify_integrity(uri, tenant_id=tenant, expected_sha256=SHA)

    assert caught.value.code == "ARTIFACT_MISSING"


def test_storage_outage_on_download_is_a_stable_code(store, client):
    tenant, job, uri = _commit(store)
    client.fail_on = "get_object"

    with pytest.raises(ArtifactIntegrityError) as caught:
        store.verify_integrity(uri, tenant_id=tenant, expected_sha256=SHA)

    assert caught.value.code == "ARTIFACT_STORAGE_UNAVAILABLE"


def test_storage_outage_on_upload_is_a_503_and_staging_can_be_discarded(store, client, staging):
    client.fail_on = "put_object"
    tenant = uuid.uuid4()
    staged = store.stage(tenant, io.BytesIO(PAYLOAD), maximum_bytes=1_000_000)

    with pytest.raises(OperationsRequestRejectedError) as caught:
        store.commit(staged, tenant_id=tenant, job_id=uuid.uuid4(), name="a.pdf")

    assert (caught.value.code, caught.value.http_status) == ("STORAGE_UNAVAILABLE", 503)
    store.discard(staged)
    assert list(staging.iterdir()) == []


def test_post_upload_verification_failure_removes_the_object(store, client):
    client.lie_about_metadata = True
    tenant = uuid.uuid4()
    staged = store.stage(tenant, io.BytesIO(PAYLOAD), maximum_bytes=1_000_000)

    with pytest.raises(ArtifactIntegrityError) as caught:
        store.commit(staged, tenant_id=tenant, job_id=uuid.uuid4(), name="a.pdf")

    assert caught.value.code == "ARTIFACT_STORE_VERIFICATION_FAILED"
    assert client.objects == {}


def test_oversized_upload_is_rejected_with_bounded_memory_and_no_residue(store, staging):
    class Endless:
        def __init__(self) -> None:
            self.served = 0

        def read(self, size):
            self.served += size
            return b"x" * size

    endless = Endless()

    with pytest.raises(OperationsRequestRejectedError) as caught:
        store.stage(uuid.uuid4(), endless, maximum_bytes=256 * 1024)

    assert caught.value.code == "FILE_TOO_LARGE"
    assert endless.served <= 256 * 1024 + 2 * 64 * 1024  # stopped right after the cap
    assert list(staging.iterdir()) == []


def test_processing_never_deletes_source_objects(store, client):
    tenant, job, uri = _commit(store)

    with store.verified_local_copy(uri, tenant_id=tenant, expected_sha256=SHA, filename="a.pdf"):
        pass

    store.verify_integrity(uri, tenant_id=tenant, expected_sha256=SHA)
    assert client.count("delete_object") == 0 and len(client.objects) == 1


def test_error_messages_never_contain_object_keys_or_hostnames(store, client):
    tenant, job, uri = _commit(store)
    client.fail_on = "get_object"

    with pytest.raises(ArtifactIntegrityError) as caught:
        store.verify_integrity(uri, tenant_id=tenant, expected_sha256=SHA)

    assert tenant.hex not in str(caught.value) and BUCKET not in str(caught.value)


def test_local_store_still_satisfies_the_new_interface(tmp_path):
    from ap_agent.artifacts.storage import LocalFilesystemArtifactStore

    local = LocalFilesystemArtifactStore(tmp_path / "root")
    tenant, job = uuid.uuid4(), uuid.uuid4()
    staged = local.stage(tenant, io.BytesIO(PAYLOAD), maximum_bytes=1_000_000)
    uri = local.commit(staged, tenant_id=tenant, job_id=job, name="a.pdf")

    local.verify_integrity(uri, tenant_id=tenant, expected_sha256=SHA)

    with local.verified_local_copy(uri, tenant_id=tenant, expected_sha256=SHA, filename="a.pdf") as path:
        assert path.read_bytes() == PAYLOAD

    assert path.exists()  # the immutable source is never removed by processing

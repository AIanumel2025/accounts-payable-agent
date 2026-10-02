"""M11E: the real boto3 client (as built for Cloudflare R2) against a mocked
S3 service. Verifies request construction, metadata and read-back; it does
not prove behaviour of the real R2 service (see the M11E report)."""

from __future__ import annotations

import hashlib
import io
import uuid

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from ap_agent.artifacts.factory import build_artifact_store
from ap_agent.artifacts.s3 import S3ArtifactStore, build_s3_client
from ap_agent.config.deployment import S3StorageConfig, StorageMode

pytestmark = pytest.mark.integration

PAYLOAD = b"\x89PNG\r\n\x1a\n" + b"data" * 4000 + b"IEND\xaeB`\x82"
SHA = hashlib.sha256(PAYLOAD).hexdigest()
CONFIG = S3StorageConfig(
    endpoint_url="https://objects.example.test", bucket="example-bucket", access_key_id="testing", secret_access_key="testing"
)


@pytest.fixture()
def mocked_store(monkeypatch):
    # moto only intercepts non-AWS hostnames when told they are S3 endpoints.
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", CONFIG.endpoint_url)

    with moto.mock_aws():
        boto3.client("s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing").create_bucket(
            Bucket=CONFIG.bucket
        )
        client = build_s3_client(CONFIG)  # exactly the client hosted mode uses (region "auto")
        yield build_artifact_store(StorageMode.S3, s3=CONFIG), client


def test_round_trip_through_a_real_boto3_client(mocked_store):
    store, client = mocked_store
    assert isinstance(store, S3ArtifactStore)
    tenant, job = uuid.uuid4(), uuid.uuid4()

    staged = store.stage(tenant, io.BytesIO(PAYLOAD), maximum_bytes=1_000_000)
    uri = store.commit(staged, tenant_id=tenant, job_id=job, name="scan.png")

    head = client.head_object(Bucket=CONFIG.bucket, Key=f"tenants/{tenant.hex}/jobs/{job.hex}/source/{SHA}")
    assert head["ContentLength"] == len(PAYLOAD)
    assert head["ContentType"] == "image/png"
    assert head["Metadata"] == {"sha256": SHA, "size": str(len(PAYLOAD))}

    with store.verified_local_copy(uri, tenant_id=tenant, expected_sha256=SHA, filename="scan.png", expected_size=len(PAYLOAD)) as path:
        assert path.read_bytes() == PAYLOAD

    assert not path.exists()
    store.verify_integrity(uri, tenant_id=tenant, expected_sha256=SHA)


def test_object_content_tampering_is_detected(mocked_store):
    store, client = mocked_store
    tenant, job = uuid.uuid4(), uuid.uuid4()
    uri = store.commit(store.stage(tenant, io.BytesIO(PAYLOAD), maximum_bytes=1_000_000), tenant_id=tenant, job_id=job, name="scan.png")

    key = f"tenants/{tenant.hex}/jobs/{job.hex}/source/{SHA}"
    client.put_object(Bucket=CONFIG.bucket, Key=key, Body=b"Z" * len(PAYLOAD), Metadata={"sha256": SHA, "size": str(len(PAYLOAD))})

    from ap_agent.exceptions import ArtifactIntegrityError

    with pytest.raises(ArtifactIntegrityError) as caught:
        store.verify_integrity(uri, tenant_id=tenant, expected_sha256=SHA)

    assert caught.value.code == "ARTIFACT_HASH_MISMATCH"


def test_bucket_is_created_private_by_default_and_objects_carry_no_acl(mocked_store):
    store, client = mocked_store
    tenant, job = uuid.uuid4(), uuid.uuid4()
    store.commit(store.stage(tenant, io.BytesIO(PAYLOAD), maximum_bytes=1_000_000), tenant_id=tenant, job_id=job, name="scan.png")

    grants = client.get_object_acl(Bucket=CONFIG.bucket, Key=f"tenants/{tenant.hex}/jobs/{job.hex}/source/{SHA}")["Grants"]
    assert all(grant["Grantee"].get("URI") is None for grant in grants)  # no AllUsers / AuthenticatedUsers grant

"""M11E.1: staged direct-to-S3 uploads -- intent issuance and finalization."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from ap_agent.artifacts.s3 import S3ArtifactStore
from ap_agent.artifacts.s3_staging import S3UploadStaging, staging_key
from ap_agent.exceptions import OperationsRequestRejectedError
from ap_agent.models.interface import InterfaceActor, InterfaceRole
from ap_agent.models.operations import JobSubmissionOutcome, UploadLimits
from ap_agent.services.direct_upload import finalize_upload, intent_idempotency_key, issue_upload_intent
from tests.support.m11e1_aws import FakeOperations, StagingS3Client

pytestmark = pytest.mark.unit

BUCKET = "ap-agent-test-uploads"
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
LIMITS = UploadLimits()
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _actor(role=InterfaceRole.AP_OPERATOR, tenant=TENANT, actor_id="user-aaaa") -> InterfaceActor:
    return InterfaceActor(actor_id=actor_id, tenant_id=tenant, role=role, authenticated_at=NOW)


@pytest.fixture()
def client() -> StagingS3Client:
    return StagingS3Client()


@pytest.fixture()
def staging(client) -> S3UploadStaging:
    return S3UploadStaging(client, BUCKET)


@pytest.fixture()
def store(client) -> S3ArtifactStore:
    return S3ArtifactStore(client, BUCKET)


def _issue(staging, *, actor=None, name="Invoice 1.pdf", payload=PDF, media="application/pdf", now=NOW):
    return issue_upload_intent(
        staging=staging, limits=LIMITS, actor=actor or _actor(), filename=name, declared_media_type=media,
        byte_size=len(payload), sha256=hashlib.sha256(payload).hexdigest(), now=now,
    )


def _policy(intent) -> dict:
    return json.loads(base64.b64decode(intent.fields["policy"]))


def _simulate_browser_upload(client, intent, payload=PDF, *, tenant=TENANT):
    """What S3 does with a valid POST: store the body under the signed key with the signed metadata."""

    fields = intent.fields
    client.put_object(
        Bucket=BUCKET, Key=fields["key"], Body=payload, ContentType=fields["Content-Type"],
        Metadata={k.removeprefix("x-amz-meta-"): v for k, v in fields.items() if k.startswith("x-amz-meta-")},
    )


# -- intent ------------------------------------------------------------------------------------


def test_policy_pins_bucket_key_type_length_and_metadata(staging):
    intent = _issue(staging)
    policy = _policy(intent)
    conditions = policy["conditions"]

    assert {"bucket": BUCKET} in conditions
    assert {"key": staging_key(TENANT.hex, intent.intent_id.hex)} in conditions
    assert ["content-length-range", len(PDF), len(PDF)] in conditions
    assert {"Content-Type": "application/pdf"} in conditions
    for name in ("tenant", "actor", "intent", "sha256", "size", "filename", "expires"):
        assert any(isinstance(c, dict) and f"x-amz-meta-{name}" in c for c in conditions), name

    expiry = datetime.fromisoformat(policy["expiration"].replace("Z", "+00:00"))
    assert expiry - datetime.now(timezone.utc) <= timedelta(minutes=5, seconds=5)


def test_staging_key_has_no_file_name_and_is_tenant_scoped(staging):
    intent = _issue(staging, name="Secret Customer Acme Ltd.pdf")
    key = intent.fields["key"]

    assert key == f"staging/{TENANT.hex}/{intent.intent_id.hex}"
    assert "Acme" not in key and ".pdf" not in key
    assert "Acme" not in intent.url


@pytest.mark.parametrize("role", [InterfaceRole.READ_ONLY_AUDITOR, InterfaceRole.AP_REVIEWER])
def test_roles_that_cannot_submit_cannot_get_an_intent(staging, client, role):
    with pytest.raises(OperationsRequestRejectedError) as raised:
        _issue(staging, actor=_actor(role))

    assert raised.value.code == "ACTION_NOT_PERMITTED" and raised.value.http_status == 403
    assert client.count("generate_presigned_post") == 0


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"name": "../evil.pdf"}, "FILENAME_INVALID"),
        ({"name": "notes.txt", "media": "text/plain"}, "UNSUPPORTED_FILE_TYPE"),
        ({"media": "image/png"}, "MEDIA_TYPE_MISMATCH"),
    ],
)
def test_declared_file_is_validated_before_any_policy_is_signed(staging, client, kwargs, code):
    with pytest.raises(OperationsRequestRejectedError) as raised:
        _issue(staging, **kwargs)

    assert raised.value.code == code
    assert client.count("generate_presigned_post") == 0


def test_declared_size_and_digest_are_validated(staging):
    base = dict(
        staging=staging, limits=LIMITS, actor=_actor(), filename="a.pdf", declared_media_type="application/pdf",
        sha256="a" * 64, now=NOW,
    )

    with pytest.raises(OperationsRequestRejectedError) as raised:
        issue_upload_intent(byte_size=LIMITS.maximum_file_bytes + 1, **base)
    assert raised.value.code == "FILE_TOO_LARGE" and raised.value.http_status == 413

    with pytest.raises(OperationsRequestRejectedError) as raised:
        issue_upload_intent(byte_size=0, **base)
    assert raised.value.code == "EMPTY_FILE"

    with pytest.raises(OperationsRequestRejectedError) as raised:
        issue_upload_intent(byte_size=10, **{**base, "sha256": "XYZ"})
    assert raised.value.code == "SHA256_INVALID"

    # The maximum supported size is accepted.
    assert issue_upload_intent(byte_size=LIMITS.maximum_file_bytes, **base).fields["key"].startswith("staging/")


# -- finalize ----------------------------------------------------------------------------------


def _finalize(staging, store, ops, intent, *, actor=None, now=NOW + timedelta(minutes=1)):
    return finalize_upload(
        repository=ops, store=store, staging=staging, limits=LIMITS, actor=actor or _actor(),
        intent_id=intent.intent_id, now=now,
    )


def test_finalize_commits_to_the_immutable_tenant_key_and_creates_one_job(staging, store, client):
    ops = FakeOperations()
    intent = _issue(staging)
    _simulate_browser_upload(client, intent)

    result = _finalize(staging, store, ops, intent)

    digest = hashlib.sha256(PDF).hexdigest()
    assert result.outcome is JobSubmissionOutcome.CREATED and ops.inserted == 1
    assert result.job.artifact_sha256 == digest and result.job.byte_size == len(PDF)
    assert result.job.source_name == "Invoice 1.pdf"
    assert result.job.idempotency_key == intent_idempotency_key(intent.intent_id)
    # Immutable, content-addressed, tenant-scoped, no customer file name.
    final_key = f"tenants/{TENANT.hex}/jobs/{result.job.job_id.hex}/source/{digest}"
    assert (BUCKET, final_key) in client.objects
    assert not any("Invoice" in key for _, key in client.objects)
    # Single use: the staging object is gone once the job exists.
    assert (BUCKET, intent.fields["key"]) not in client.objects


def test_finalize_twice_replays_the_same_job_and_never_duplicates(staging, store, client):
    ops = FakeOperations()
    intent = _issue(staging)
    _simulate_browser_upload(client, intent)

    first = _finalize(staging, store, ops, intent)
    second = _finalize(staging, store, ops, intent)

    assert second.outcome is JobSubmissionOutcome.IDEMPOTENT_REPLAY
    assert second.job.job_id == first.job.job_id and ops.inserted == 1


def test_another_actor_cannot_finalize_or_replay_someone_elses_intent(staging, store, client):
    ops = FakeOperations()
    intent = _issue(staging)
    _simulate_browser_upload(client, intent)
    intruder = _actor(actor_id="user-bbbb")

    with pytest.raises(OperationsRequestRejectedError) as raised:
        _finalize(staging, store, ops, intent, actor=intruder)
    assert raised.value.code == "UPLOAD_INTENT_NOT_FOUND" and raised.value.http_status == 404

    _finalize(staging, store, ops, intent)  # the owner can

    with pytest.raises(OperationsRequestRejectedError) as raised:  # replay is owner-only too
        _finalize(staging, store, ops, intent, actor=intruder)
    assert raised.value.code == "UPLOAD_INTENT_NOT_FOUND"


def test_cross_tenant_intent_does_not_exist(staging, store, client):
    ops = FakeOperations()
    intent = _issue(staging)
    _simulate_browser_upload(client, intent)

    with pytest.raises(OperationsRequestRejectedError) as raised:
        _finalize(staging, store, ops, intent, actor=_actor(tenant=OTHER_TENANT))

    assert raised.value.code == "UPLOAD_INTENT_NOT_FOUND" and ops.inserted == 0


def test_substituted_object_metadata_is_refused(staging, store, client):
    """An object copied to another tenant's key (with the first tenant's metadata) is not accepted."""

    ops = FakeOperations()
    intent = _issue(staging)
    _simulate_browser_upload(client, intent)
    foreign_key = staging_key(OTHER_TENANT.hex, intent.intent_id.hex)
    client.objects[(BUCKET, foreign_key)] = dict(client.objects[(BUCKET, intent.fields["key"])])

    with pytest.raises(OperationsRequestRejectedError) as raised:
        _finalize(staging, store, ops, intent, actor=_actor(tenant=OTHER_TENANT))

    assert raised.value.code == "UPLOAD_INTENT_NOT_FOUND" and ops.inserted == 0


def test_missing_upload_is_not_found(staging, store):
    ops = FakeOperations()
    intent = _issue(staging)

    with pytest.raises(OperationsRequestRejectedError) as raised:
        _finalize(staging, store, ops, intent)

    assert raised.value.code == "UPLOAD_INTENT_NOT_FOUND"


def test_expired_intent_is_refused_and_cleaned_up(staging, store, client):
    ops = FakeOperations()
    intent = _issue(staging)
    _simulate_browser_upload(client, intent)

    with pytest.raises(OperationsRequestRejectedError) as raised:
        _finalize(staging, store, ops, intent, now=NOW + timedelta(minutes=31))

    assert raised.value.code == "UPLOAD_INTENT_EXPIRED" and raised.value.http_status == 410
    assert ops.inserted == 0 and (BUCKET, intent.fields["key"]) not in client.objects


def test_bytes_that_differ_from_the_declared_digest_are_refused(staging, store, client):
    ops = FakeOperations()
    intent = _issue(staging)
    _simulate_browser_upload(client, intent, payload=PDF.replace(b"Catalog", b"Katalog"))  # same length

    with pytest.raises(OperationsRequestRejectedError) as raised:
        _finalize(staging, store, ops, intent)

    assert raised.value.code == "UPLOAD_DIGEST_MISMATCH" and ops.inserted == 0
    assert not any(key.startswith("tenants/") for _, key in client.objects)


def test_size_that_differs_from_the_declared_size_is_refused(staging, store, client):
    ops = FakeOperations()
    intent = _issue(staging)
    _simulate_browser_upload(client, intent, payload=PDF + b"extra")

    with pytest.raises(OperationsRequestRejectedError) as raised:
        _finalize(staging, store, ops, intent)

    assert raised.value.code == "UPLOAD_METADATA_MISMATCH" and ops.inserted == 0


def test_wrong_file_signature_is_refused_even_with_a_matching_digest(staging, store, client):
    ops = FakeOperations()
    payload = b"MZ" + b"\x00" * 64 + b"%%EOF"  # an executable that calls itself a PDF
    intent = _issue(staging, payload=payload)
    _simulate_browser_upload(client, intent, payload=payload)

    with pytest.raises(OperationsRequestRejectedError) as raised:
        _finalize(staging, store, ops, intent)

    assert raised.value.code in {"UNSUPPORTED_FILE_TYPE", "FILE_SIGNATURE_MISMATCH"} and ops.inserted == 0
    assert not any(key.startswith("tenants/") for _, key in client.objects)


def test_tampered_metadata_content_type_is_refused(staging, store, client):
    ops = FakeOperations()
    intent = _issue(staging)
    _simulate_browser_upload(client, intent)
    client.objects[(BUCKET, intent.fields["key"])]["ContentType"] = "text/html"

    with pytest.raises(OperationsRequestRejectedError) as raised:
        _finalize(staging, store, ops, intent)

    assert raised.value.code == "MEDIA_TYPE_MISMATCH" and ops.inserted == 0


def test_finalize_requires_submit_permission(staging, store, client):
    ops = FakeOperations()
    intent = _issue(staging)
    _simulate_browser_upload(client, intent)

    with pytest.raises(OperationsRequestRejectedError) as raised:
        _finalize(staging, store, ops, intent, actor=_actor(InterfaceRole.READ_ONLY_AUDITOR))

    assert raised.value.code == "ACTION_NOT_PERMITTED" and ops.inserted == 0


def test_storage_outage_while_finalizing_is_a_503_and_keeps_the_staged_object(staging, store, client):
    ops = FakeOperations()
    intent = _issue(staging)
    _simulate_browser_upload(client, intent)
    client.fail_on = "head_object"

    with pytest.raises(OperationsRequestRejectedError) as raised:
        _finalize(staging, store, ops, intent)

    assert raised.value.http_status == 503 and ops.inserted == 0
    client.fail_on = None
    assert _finalize(staging, store, ops, intent).outcome is JobSubmissionOutcome.CREATED  # retry works

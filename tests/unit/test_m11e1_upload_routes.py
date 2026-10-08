"""M11E.1: the staged-upload HTTP routes, through the IAM-platform auth boundary."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from ap_agent.api.config import ApiConfig
from ap_agent.api.dependencies import CLERK_AUTHORIZATION_HEADER
from ap_agent.api.routes.operations import get_operations_repository
from ap_agent.artifacts.s3 import S3ArtifactStore
from ap_agent.artifacts.s3_staging import S3UploadStaging
from ap_agent.config.deployment import AuthMode, AwsS3Config, DeploymentEnvironment, PlatformAuthMode, StorageMode
from ap_agent.models.interface import InterfaceRole
from ap_agent.services.job_dispatch import SqsJobDispatcher
from tests.support.m11e1_aws import FakeOperations, StagingS3Client
from tests.support.m11e_auth import TestKey, clerk_config, static_verifier
from tests.unit.test_m11e1_queue_worker import FakeSqs
from tests.unit.test_m11e_auth_dependency import FakeIdentities

pytestmark = pytest.mark.unit

BUCKET = "ap-agent-test-uploads"
TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
INTENTS = "/api/v1/operations/upload-intents"


class Harness:
    def __init__(self) -> None:
        from ap_agent.api.app import create_app

        self.key = TestKey()
        self.s3 = StagingS3Client()
        self.sqs = FakeSqs()
        self.operations = FakeOperations()
        self.identities = FakeIdentities()
        self.identities.add("org_a", "operator", TENANT_A, InterfaceRole.AP_OPERATOR)
        self.identities.add("org_a", "auditor", TENANT_A, InterfaceRole.READ_ONLY_AUDITOR)
        self.identities.add("org_b", "operator", TENANT_B, InterfaceRole.AP_OPERATOR)

        config = ApiConfig(
            environment=DeploymentEnvironment.HOSTED, auth_mode=AuthMode.CLERK_JWT,
            clerk=clerk_config(issuer="https://clerk.example.test", authorized_parties=("https://app.example.test",)),
            platform_auth_mode=PlatformAuthMode.AWS_SIGV4, storage_mode=StorageMode.AWS_S3,
            aws_s3=AwsS3Config(bucket=BUCKET, region="eu-west-2"), enable_operations=True,
        )
        app = create_app(
            dsn="postgresql://unused.invalid/db", api_config=config, clerk_verifier=static_verifier(self.key),
            artifact_store=S3ArtifactStore(self.s3, BUCKET), upload_staging=S3UploadStaging(self.s3, BUCKET),
            job_dispatcher=SqsJobDispatcher(self.sqs, "https://sqs.eu-west-2.amazonaws.com/0/q.fifo"),
        )
        app.state.identity_repository = self.identities
        app.dependency_overrides[get_operations_repository] = lambda: self.operations
        self.client = TestClient(app)

    def headers(self, org="org_a", user="operator") -> dict[str, str]:
        return {CLERK_AUTHORIZATION_HEADER: f"Bearer {self.key.mint(org=org, user=user)}"}

    def intent(self, **headers):
        body = {
            "filename": "Invoice 1.pdf", "media_type": "application/pdf", "byte_size": len(PDF),
            "sha256": hashlib.sha256(PDF).hexdigest(),
        }
        return self.client.post(INTENTS, json=body, headers=headers or self.headers())

    def upload(self, data: dict) -> None:
        fields = data["upload_fields"]
        self.s3.put_object(
            Bucket=BUCKET, Key=fields["key"], Body=PDF, ContentType=fields["Content-Type"],
            Metadata={k.removeprefix("x-amz-meta-"): v for k, v in fields.items() if k.startswith("x-amz-meta-")},
        )


@pytest.fixture()
def harness() -> Harness:
    return Harness()


def test_full_staged_flow_returns_without_ocr_and_dispatches_one_message(harness):
    created = harness.intent()
    assert created.status_code == 201
    data = created.json()["data"]
    assert data["upload_url"].startswith("https://") and data["maximum_bytes"] == 10 * 1024 * 1024
    assert "Invoice" not in json.dumps(data) and "AKIA" not in data["upload_fields"].get("key", "")

    harness.upload(data)
    finalized = harness.client.post(f"{INTENTS}/{data['intent_id']}/finalize", headers=harness.headers())

    assert finalized.status_code == 202
    job = finalized.json()["data"]["job"]
    assert job["status"] == "QUEUED"  # nothing ran synchronously
    assert [json.loads(m["MessageBody"])["job_id"] for m in harness.sqs.sent] == [job["job_id"]]
    assert "artifact_uri" not in job and "object://" not in json.dumps(finalized.json())

    again = harness.client.post(f"{INTENTS}/{data['intent_id']}/finalize", headers=harness.headers())
    assert again.status_code == 200 and again.json()["data"]["idempotent_replay"] is True
    assert harness.operations.inserted == 1


def test_auditor_can_neither_get_an_intent_nor_finalize(harness):
    refused = harness.intent(**harness.headers(user="auditor"))
    assert refused.status_code == 403 and refused.json()["errors"] == ["ACTION_NOT_PERMITTED"]

    data = harness.intent().json()["data"]
    harness.upload(data)
    response = harness.client.post(f"{INTENTS}/{data['intent_id']}/finalize", headers=harness.headers(user="auditor"))
    assert response.status_code == 403 and harness.operations.inserted == 0


def test_another_organizations_member_cannot_finalize_the_intent(harness):
    data = harness.intent().json()["data"]
    harness.upload(data)

    response = harness.client.post(f"{INTENTS}/{data['intent_id']}/finalize", headers=harness.headers("org_b"))

    assert response.status_code == 404 and response.json()["errors"] == ["UPLOAD_INTENT_NOT_FOUND"]
    assert harness.operations.inserted == 0 and harness.sqs.sent == []


def test_unauthenticated_and_forged_requests_are_refused(harness):
    body = {"filename": "a.pdf", "media_type": "application/pdf", "byte_size": 3, "sha256": "a" * 64}

    assert harness.client.post(INTENTS, json=body).status_code == 401
    forged = {"X-Tenant-ID": str(TENANT_A), "X-Actor-ID": "x", "X-Actor-Role": "TENANT_ADMIN"}
    assert harness.client.post(INTENTS, json=body, headers=forged).status_code == 401
    assert harness.s3.count("generate_presigned_post") == 0


def test_request_body_cannot_carry_identity_or_extra_fields(harness):
    body = {
        "filename": "a.pdf", "media_type": "application/pdf", "byte_size": 3, "sha256": "a" * 64,
        "tenant_id": str(TENANT_B),
    }

    assert harness.client.post(INTENTS, json=body, headers=harness.headers()).status_code == 422


def test_direct_upload_is_unavailable_outside_native_s3_storage(tmp_path):
    from ap_agent.api.app import create_app

    config = ApiConfig(enable_operations=True, artifact_root=tmp_path)
    app = create_app(dsn="postgresql://unused.invalid/db", api_config=config)
    headers = {
        "X-Tenant-ID": str(TENANT_A), "X-Actor-ID": "dev", "X-Actor-Role": "AP_OPERATOR",
    }

    response = TestClient(app).post(
        INTENTS, headers=headers,
        json={"filename": "a.pdf", "media_type": "application/pdf", "byte_size": 3, "sha256": "a" * 64},
    )

    assert response.status_code == 404 and response.json()["errors"] == ["DIRECT_UPLOAD_UNAVAILABLE"]


def test_dispatch_outage_after_commit_is_retryable_and_never_duplicates_the_job(harness):
    data = harness.intent().json()["data"]
    harness.upload(data)
    harness.sqs.fail = True

    failed = harness.client.post(f"{INTENTS}/{data['intent_id']}/finalize", headers=harness.headers())
    assert failed.status_code == 503 and failed.json()["errors"] == ["DISPATCH_UNAVAILABLE"]
    assert harness.operations.inserted == 1

    harness.sqs.fail = False
    retried = harness.client.post(f"{INTENTS}/{data['intent_id']}/finalize", headers=harness.headers())

    assert retried.status_code == 200 and harness.operations.inserted == 1 and len(harness.sqs.sent) == 1

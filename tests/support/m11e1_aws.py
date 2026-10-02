"""Test doubles for the M11E.1 AWS paths (no network, no AWS account)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
from botocore.config import Config

from ap_agent.models.operations import WorkflowJob, WorkflowJobStatus, WorkflowJobType
from tests.support.fake_s3 import FakeS3Client


class StagingS3Client(FakeS3Client):
    """In-memory objects plus a *real* boto3 presigner (signing is local;
    credentials are throwaway test values)."""

    def __init__(self) -> None:
        super().__init__()
        self._presigner = boto3.client(
            "s3",
            region_name="eu-west-2",
            aws_access_key_id="TESTACCESSKEYID",
            aws_secret_access_key="test-signing-material-not-real",
            config=Config(signature_version="s3v4"),
        )

    def generate_presigned_post(self, **kwargs: Any) -> dict[str, Any]:
        self._record("generate_presigned_post", **{k: v for k, v in kwargs.items() if k != "Fields"})
        return self._presigner.generate_presigned_post(**kwargs)


class FakeOperations:
    """The two repository methods the upload paths use."""

    def __init__(self) -> None:
        self.jobs: dict[tuple[uuid.UUID, str, str], WorkflowJob] = {}
        self.inserted = 0

    def find_job_by_idempotency(self, tenant_id, job_type, idempotency_key) -> Optional[WorkflowJob]:
        return self.jobs.get((tenant_id, job_type.value, idempotency_key))

    def insert_upload_job(self, **kw: Any) -> WorkflowJob:
        self.inserted += 1
        now = datetime.now(timezone.utc)
        job = WorkflowJob(
            job_id=kw["job_id"], tenant_id=kw["tenant_id"], job_type=WorkflowJobType.PROCESS_DOCUMENT,
            idempotency_key=kw["idempotency_key"], request_fingerprint=kw["request_fingerprint"],
            source_name=kw["source_name"], batch_id=None, workflow_id=None, document_id=None, review_id=None,
            resume_plan_id=None, artifact_uri=kw["artifact_uri"], artifact_sha256=kw["artifact_sha256"],
            media_type=kw["media_type"], byte_size=kw["byte_size"], status=WorkflowJobStatus.QUEUED,
            current_stage=None, attempt_count=0, max_attempts=1, error_code=None, result_summary={},
            result_review_id=None, result_version_id=None, created_by=kw["created_by"], created_at=now,
            updated_at=now, started_at=None, completed_at=None, lock_version=0,
        )
        self.jobs[(kw["tenant_id"], "PROCESS_DOCUMENT", kw["idempotency_key"])] = job
        return job

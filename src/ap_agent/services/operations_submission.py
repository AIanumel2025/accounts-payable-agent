"""Upload submission for the operations console (M11D Core).

The control plane validates, stores the artifact and enqueues a job; it
never runs OCR or orchestration. Idempotency: the same key with the same
content returns the existing job; the same key with different content is a
conflict (`409`). Cleanup: a staged or committed artifact is removed if the
database write fails or loses an idempotency race.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import BinaryIO, Optional
from uuid import UUID, uuid4

from ap_agent.artifacts.storage import ArtifactStore
from ap_agent.artifacts.upload_validation import validate_upload_content, validate_upload_filename
from ap_agent.exceptions import OperationsRequestRejectedError
from ap_agent.models.interface import InterfaceActor, InterfaceRole
from ap_agent.models.operations import (
    JobSubmissionOutcome,
    SubmissionResult,
    UploadLimits,
    WorkflowJob,
    WorkflowJobType,
)
from ap_agent.repositories.operations_repository import IdempotencyRaceError, OperationsRepository
from ap_agent.services.review_commands import IDEMPOTENCY_KEY_PATTERN

__all__ = [
    "SUBMIT_ROLES",
    "READ_ROLES",
    "require_submit_permission",
    "require_read_permission",
    "submission_fingerprint",
    "submit_upload",
]

# Declared role policy (M11D Core): operators and tenant administrators
# submit uploads; reviewers and read-only auditors may read the queue (they
# need it to follow a case) but never submit. Resume commands keep M11C's
# own policy (reviewer/admin).
SUBMIT_ROLES = frozenset({InterfaceRole.AP_OPERATOR, InterfaceRole.TENANT_ADMIN})
READ_ROLES = frozenset(InterfaceRole)


def require_submit_permission(actor: InterfaceActor) -> None:
    if actor.role not in SUBMIT_ROLES:
        raise OperationsRequestRejectedError("ACTION_NOT_PERMITTED", http_status=403)


def require_read_permission(actor: InterfaceActor) -> None:
    if actor.role not in READ_ROLES:
        raise OperationsRequestRejectedError("ACTION_NOT_PERMITTED", http_status=403)


def submission_fingerprint(*, artifact_sha256: str, media_type: str, byte_size: int, source_name: str) -> str:
    canonical = json.dumps(
        {
            "job_type": WorkflowJobType.PROCESS_DOCUMENT.value,
            "artifact_sha256": artifact_sha256,
            "media_type": media_type,
            "byte_size": byte_size,
            "source_name": source_name,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    return sha256(canonical.encode("utf-8")).hexdigest()


def _replay_or_conflict(existing: WorkflowJob, fingerprint: str) -> SubmissionResult:
    if existing.request_fingerprint != fingerprint:
        raise OperationsRequestRejectedError("IDEMPOTENCY_KEY_CONTENT_CONFLICT", http_status=409)

    return SubmissionResult(outcome=JobSubmissionOutcome.IDEMPOTENT_REPLAY, job=existing)


def submit_upload(
    *,
    repository: OperationsRepository,
    store: ArtifactStore,
    limits: UploadLimits,
    actor: InterfaceActor,
    stream: BinaryIO,
    filename: Optional[str],
    declared_media_type: Optional[str],
    idempotency_key: Optional[str],
) -> SubmissionResult:
    require_submit_permission(actor)

    if idempotency_key is None or not idempotency_key.strip():
        raise OperationsRequestRejectedError("IDEMPOTENCY_KEY_MISSING")

    if IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None:
        raise OperationsRequestRejectedError("IDEMPOTENCY_KEY_INVALID")

    name = validate_upload_filename(filename, limits)
    tenant_id: UUID = actor.tenant_id

    staged = store.stage(tenant_id, stream, maximum_bytes=limits.maximum_file_bytes)
    artifact_uri: Optional[str] = None

    try:
        validate_upload_content(
            name=name, declared_media_type=declared_media_type, head=staged.head, tail=staged.tail,
            size=staged.size, limits=limits,
        )

        fingerprint = submission_fingerprint(
            artifact_sha256=staged.sha256, media_type=name.media_type, byte_size=staged.size,
            source_name=name.display_name,
        )

        existing = repository.find_job_by_idempotency(tenant_id, WorkflowJobType.PROCESS_DOCUMENT, idempotency_key)

        if existing is not None:
            store.discard(staged)
            return _replay_or_conflict(existing, fingerprint)

        job_id = uuid4()
        artifact_uri = store.commit(staged, tenant_id=tenant_id, job_id=job_id, name=name.display_name)

        try:
            job = repository.insert_upload_job(
                tenant_id=tenant_id, job_id=job_id, idempotency_key=idempotency_key,
                request_fingerprint=fingerprint, source_name=name.display_name, artifact_uri=artifact_uri,
                artifact_sha256=staged.sha256, media_type=name.media_type, byte_size=staged.size,
                created_by=actor.actor_id,
            )
        except IdempotencyRaceError:
            store.delete(artifact_uri)
            artifact_uri = None
            winner = repository.find_job_by_idempotency(tenant_id, WorkflowJobType.PROCESS_DOCUMENT, idempotency_key)

            if winner is None:  # pragma: no cover - the unique violation guarantees a winner
                raise
            return _replay_or_conflict(winner, fingerprint)

        artifact_uri = None  # ownership passed to the committed job
        return SubmissionResult(outcome=JobSubmissionOutcome.CREATED, job=job)
    except BaseException:
        store.discard(staged)

        if artifact_uri is not None:
            store.delete(artifact_uri)

        raise

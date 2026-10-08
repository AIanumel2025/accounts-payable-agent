"""Staged direct-to-S3 uploads: intent issuance and finalization (M11E.1).

    1. `issue_upload_intent`   validates the *declared* file, returns a short-lived
                               presigned POST for an exact staging key.
    2. (browser uploads straight to S3)
    3. `finalize_upload`       re-validates everything from the stored object and
                               only then creates the job through the unchanged
                               `submit_upload` path (content-addressed, tenant-
                               scoped immutable key; job row; idempotent).

Finalization never trusts the browser: tenant and actor come from the verified
identity; the staging object must sit under that tenant's own prefix, carry
metadata bound to that tenant, actor and intent, still be inside its finalize
window, match its declared size, content type and SHA-256 byte for byte, and
pass the same extension / media-type / file-signature checks as a multipart
upload. A second finalize of the same intent replays the first job (the
idempotency key is derived from the intent id) and never creates another.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import datetime
from hashlib import sha256 as _sha256
from pathlib import Path
from typing import Optional
from uuid import UUID, uuid4

from ap_agent.artifacts.s3_staging import (
    SHA256_PATTERN,
    S3UploadStaging,
    UploadIntent,
    decode_display_name,
    utc_now,
)
from ap_agent.artifacts.storage import ArtifactStore
from ap_agent.artifacts.upload_validation import validate_upload_filename
from ap_agent.exceptions import OperationsRequestRejectedError
from ap_agent.models.interface import InterfaceActor
from ap_agent.models.operations import JobSubmissionOutcome, SubmissionResult, UploadLimits, WorkflowJobType
from ap_agent.repositories.operations_repository import OperationsRepository
from ap_agent.services.operations_submission import require_submit_permission, submit_upload

__all__ = [
    "intent_idempotency_key",
    "issue_upload_intent",
    "finalize_upload",
]

_LOGGER = logging.getLogger("ap_agent.direct_upload")
_CHUNK_BYTES = 64 * 1024


def intent_idempotency_key(intent_id: UUID) -> str:
    """One intent, one job: finalization's idempotency key is a pure function
    of the intent id."""

    return f"ap-direct-{intent_id.hex}"


def issue_upload_intent(
    *,
    staging: S3UploadStaging,
    limits: UploadLimits,
    actor: InterfaceActor,
    filename: Optional[str],
    declared_media_type: Optional[str],
    byte_size: int,
    sha256: str,
    now: Optional[datetime] = None,
) -> UploadIntent:
    require_submit_permission(actor)

    name = validate_upload_filename(filename, limits)

    if byte_size <= 0:
        raise OperationsRequestRejectedError("EMPTY_FILE")

    if byte_size > limits.maximum_file_bytes:
        raise OperationsRequestRejectedError("FILE_TOO_LARGE", http_status=413)

    if (declared_media_type or "").split(";")[0].strip().lower() != name.media_type:
        raise OperationsRequestRejectedError("MEDIA_TYPE_MISMATCH", http_status=415)

    if SHA256_PATTERN.fullmatch(sha256 or "") is None:
        raise OperationsRequestRejectedError("SHA256_INVALID")

    return staging.create_intent(
        tenant_id=actor.tenant_id,
        actor_id=actor.actor_id,
        intent_id=uuid4(),
        display_name=name.display_name,
        media_type=name.media_type,
        byte_size=byte_size,
        sha256=sha256,
        now=now or utc_now(),
    )


def _not_found() -> OperationsRequestRejectedError:
    return OperationsRequestRejectedError("UPLOAD_INTENT_NOT_FOUND", http_status=404)


def finalize_upload(
    *,
    repository: OperationsRepository,
    store: ArtifactStore,
    staging: S3UploadStaging,
    limits: UploadLimits,
    actor: InterfaceActor,
    intent_id: UUID,
    now: Optional[datetime] = None,
) -> SubmissionResult:
    require_submit_permission(actor)

    tenant_hex = actor.tenant_id.hex
    intent_hex = intent_id.hex
    idempotency_key = intent_idempotency_key(intent_id)

    existing = repository.find_job_by_idempotency(actor.tenant_id, WorkflowJobType.PROCESS_DOCUMENT, idempotency_key)

    if existing is not None:
        # Replay: only for the actor who created it. Anyone else sees "not found".
        if existing.created_by != actor.actor_id:
            raise _not_found()

        return SubmissionResult(outcome=JobSubmissionOutcome.IDEMPOTENT_REPLAY, job=existing)

    # The staging key is derived from the *authenticated* tenant, so another
    # tenant's intent id simply does not exist here.
    staged = staging.head(tenant_hex, intent_hex)

    if staged is None:
        raise _not_found()

    metadata = staged.metadata

    if (
        metadata.get("tenant") != tenant_hex
        or metadata.get("intent") != intent_hex
        or metadata.get("actor") != actor.actor_id
    ):
        raise _not_found()

    try:
        expires_at = int(metadata.get("expires", ""))
    except ValueError:
        raise _not_found() from None

    if (now or utc_now()).timestamp() > expires_at:
        staging.delete(tenant_hex, intent_hex)
        raise OperationsRequestRejectedError("UPLOAD_INTENT_EXPIRED", http_status=410)

    declared_sha256 = metadata.get("sha256", "")
    display_name = decode_display_name(metadata.get("filename", ""))

    if (
        SHA256_PATTERN.fullmatch(declared_sha256) is None
        or display_name is None
        or metadata.get("size") != str(staged.size)
        or staged.size <= 0
    ):
        raise OperationsRequestRejectedError("UPLOAD_METADATA_MISMATCH")

    if staged.size > limits.maximum_file_bytes:
        staging.delete(tenant_hex, intent_hex)
        raise OperationsRequestRejectedError("FILE_TOO_LARGE", http_status=413)

    name = validate_upload_filename(display_name, limits)

    if staged.content_type.split(";")[0].strip().lower() != name.media_type:
        raise OperationsRequestRejectedError("MEDIA_TYPE_MISMATCH", http_status=415)

    # Copy the staged bytes into a private temporary file while hashing them,
    # so the bytes that are hashed are the bytes that get committed (the
    # presigned POST could otherwise overwrite the staging object in between).
    directory = Path(tempfile.mkdtemp(prefix="ap-agent-finalize-"))

    try:
        local = directory / "staged"
        digest = _sha256()
        received = 0
        body = staging.open_body(tenant_hex, intent_hex)

        try:
            with local.open("wb") as destination:
                for chunk in iter(lambda: body.read(_CHUNK_BYTES), b""):
                    received += len(chunk)

                    if received > staged.size:
                        raise OperationsRequestRejectedError("UPLOAD_METADATA_MISMATCH")

                    digest.update(chunk)
                    destination.write(chunk)
        finally:
            close = getattr(body, "close", None)

            if callable(close):
                close()

        if received != staged.size or digest.hexdigest() != declared_sha256:
            staging.delete(tenant_hex, intent_hex)
            raise OperationsRequestRejectedError("UPLOAD_DIGEST_MISMATCH")

        try:
            with local.open("rb") as stream:
                result = submit_upload(
                    repository=repository,
                    store=store,
                    limits=limits,
                    actor=actor,
                    stream=stream,
                    filename=name.display_name,
                    declared_media_type=name.media_type,
                    idempotency_key=idempotency_key,
                )
        except OperationsRequestRejectedError as error:
            if error.http_status != 503:  # a definitive rejection: the staged bytes are not usable
                staging.delete(tenant_hex, intent_hex)

            raise
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    staging.delete(tenant_hex, intent_hex)
    return result

"""S3-compatible (Cloudflare R2) artifact store (M11E).

Satisfies `ap_agent.artifacts.storage.ArtifactStore`. Everything below is
server-side only: the bucket is private, no public or pre-signed URL is ever
created or returned, and credentials live only in `S3StorageConfig` (excluded
from `repr`). The database stores an opaque reference

    object://<tenant-hex>/<job-hex>/<sha256>

and the object key is derived from those three validated components alone:

    tenants/<tenant-hex>/jobs/<job-hex>/source/<sha256>

so the submitted filename is never part of a key, a key can never contain a
path separator or `..`, and a reference naming another tenant is refused
before any request is made. Objects are content-addressed under their job:
re-committing the same bytes for the same job is an idempotent no-op.

Integrity: size, content type and SHA-256 are sent with the upload (S3
`ChecksumSHA256` plus user metadata), read back with `head_object`, and the
worker recomputes the SHA-256 of the downloaded bytes against the value
recorded in PostgreSQL before any pipeline stage runs. Uploads are first
spooled to a private temporary file (bounded memory, size-capped) exactly as
for the local store. Downloads go to a `0700` temporary directory that is
removed in `finally`. Source invoices are never deleted by workflow
processing; `delete` exists only to roll back a submission whose database
write failed.

`boto3` is imported lazily by `build_s3_client`; the store itself takes any
client exposing `head_object`, `put_object`, `get_object`, `delete_object`,
so tests inject a fake or a mocked S3 service.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional
from uuid import UUID

from ap_agent.artifacts.storage import (
    StagedArtifact,
    safe_artifact_filename,
    spool_stream_to_file,
)
from ap_agent.config.deployment import AwsS3Config, S3StorageConfig
from ap_agent.exceptions import ArtifactIntegrityError, OperationsRequestRejectedError

__all__ = [
    "OBJECT_URI_PREFIX",
    "parse_object_uri",
    "object_key_for",
    "S3ArtifactStore",
    "build_s3_client",
    "build_aws_s3_client",
]

OBJECT_URI_PREFIX = "object://"

_OBJECT_URI_PATTERN = re.compile(
    r"^object://(?P<tenant>[0-9a-f]{32})/(?P<job>[0-9a-f]{32})/(?P<sha>[0-9a-f]{64})$"
)

_CHUNK_BYTES = 64 * 1024

_EXTENSION_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

_LOGGER = logging.getLogger("ap_agent.artifacts")


def parse_object_uri(artifact_uri: str) -> tuple[str, str, str]:
    match = _OBJECT_URI_PATTERN.fullmatch(artifact_uri)

    if match is None:
        raise ArtifactIntegrityError("ARTIFACT_REFERENCE_INVALID")

    return match.group("tenant"), match.group("job"), match.group("sha")


def object_key_for(tenant_hex: str, job_hex: str, sha256_hex: str) -> str:
    return f"tenants/{tenant_hex}/jobs/{job_hex}/source/{sha256_hex}"


def _content_type(name: str) -> str:
    return _EXTENSION_MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def _is_missing(error: BaseException) -> bool:
    response = getattr(error, "response", None)

    if not isinstance(response, dict):
        return False

    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = response.get("Error", {}).get("Code")

    return status == 404 or code in {"404", "NoSuchKey", "NotFound"}


class _NullSink:
    """Hash-only verification: bytes are read and discarded."""

    @staticmethod
    def write(chunk: bytes) -> int:
        return len(chunk)


class S3ArtifactStore:
    def __init__(self, client: Any, bucket: str, *, staging_directory: Optional[Path] = None) -> None:
        self._client = client
        self._bucket = bucket
        self._staging_directory = staging_directory

    # -- helpers ---------------------------------------------------------

    def _new_private_directory(self, prefix: str) -> Path:
        base = str(self._staging_directory) if self._staging_directory is not None else None
        return Path(tempfile.mkdtemp(prefix=prefix, dir=base))  # mode 0700

    def _head(self, key: str) -> Optional[dict[str, Any]]:
        try:
            return self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception as error:  # noqa: BLE001 - classify, never leak the message
            if _is_missing(error):
                return None

            _LOGGER.error("Object storage request failed (%s).", type(error).__name__)
            raise _unavailable() from error

    # -- staging ---------------------------------------------------------

    def stage(self, tenant_id: UUID, stream: BinaryIO, *, maximum_bytes: int) -> StagedArtifact:
        directory = self._new_private_directory("ap-agent-upload-")

        try:
            return spool_stream_to_file(directory, stream, maximum_bytes=maximum_bytes)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def discard(self, staged: StagedArtifact) -> None:
        staged.staging_path.unlink(missing_ok=True)

        try:
            staged.staging_path.parent.rmdir()
        except OSError:
            pass

    # -- commit ----------------------------------------------------------

    def commit(self, staged: StagedArtifact, *, tenant_id: UUID, job_id: UUID, name: str) -> str:
        artifact_uri = f"{OBJECT_URI_PREFIX}{tenant_id.hex}/{job_id.hex}/{staged.sha256}"
        tenant_hex, job_hex, sha = parse_object_uri(artifact_uri)
        key = object_key_for(tenant_hex, job_hex, sha)
        content_type = _content_type(name)

        existing = self._head(key)

        if existing is None or not self._matches(existing, size=staged.size, sha256=staged.sha256):
            try:
                with staged.staging_path.open("rb") as body:
                    self._client.put_object(
                        Bucket=self._bucket,
                        Key=key,
                        Body=body,
                        ContentLength=staged.size,
                        ContentType=content_type,
                        ChecksumSHA256=base64.b64encode(bytes.fromhex(staged.sha256)).decode("ascii"),
                        Metadata={"sha256": staged.sha256, "size": str(staged.size)},
                    )
            except Exception as error:  # noqa: BLE001
                _LOGGER.error("Object storage upload failed (%s).", type(error).__name__)
                raise _unavailable() from error

            stored = self._head(key)

            if stored is None or not self._matches(stored, size=staged.size, sha256=staged.sha256):
                self.delete(artifact_uri)
                raise ArtifactIntegrityError("ARTIFACT_STORE_VERIFICATION_FAILED")

        self.discard(staged)
        return artifact_uri

    @staticmethod
    def _matches(head: dict[str, Any], *, size: int, sha256: str) -> bool:
        metadata = {str(k).lower(): str(v) for k, v in (head.get("Metadata") or {}).items()}

        return head.get("ContentLength") == size and metadata.get("sha256") == sha256

    # -- retrieval -------------------------------------------------------

    def _checked_key(self, artifact_uri: str, tenant_id: UUID, expected_sha256: str) -> tuple[str, str]:
        tenant_hex, job_hex, sha = parse_object_uri(artifact_uri)

        if tenant_hex != tenant_id.hex:
            raise ArtifactIntegrityError("ARTIFACT_TENANT_MISMATCH")

        expected = expected_sha256.strip().lower()

        if sha != expected:
            raise ArtifactIntegrityError("ARTIFACT_HASH_MISMATCH")

        return object_key_for(tenant_hex, job_hex, sha), expected

    def _download(
        self, key: str, destination: BinaryIO, *, expected_sha256: str, expected_size: Optional[int]
    ) -> None:
        try:
            head = self._head(key)
        except OperationsRequestRejectedError as error:
            raise ArtifactIntegrityError("ARTIFACT_STORAGE_UNAVAILABLE") from error

        if head is None:
            raise ArtifactIntegrityError("ARTIFACT_MISSING")

        declared_size = head.get("ContentLength")

        if not isinstance(declared_size, int) or (expected_size is not None and declared_size != expected_size):
            raise ArtifactIntegrityError("ARTIFACT_SIZE_MISMATCH")

        metadata = {str(k).lower(): str(v) for k, v in (head.get("Metadata") or {}).items()}

        if metadata.get("sha256") not in (None, expected_sha256):
            raise ArtifactIntegrityError("ARTIFACT_METADATA_MISMATCH")

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"]
        except Exception as error:  # noqa: BLE001
            if _is_missing(error):
                raise ArtifactIntegrityError("ARTIFACT_MISSING") from error

            _LOGGER.error("Object storage download failed (%s).", type(error).__name__)
            raise ArtifactIntegrityError("ARTIFACT_STORAGE_UNAVAILABLE") from error

        digest = hashlib.sha256()
        received = 0

        try:
            for chunk in iter(lambda: body.read(_CHUNK_BYTES), b""):
                received += len(chunk)

                if received > declared_size:  # never write more than the verified size
                    raise ArtifactIntegrityError("ARTIFACT_SIZE_MISMATCH")

                digest.update(chunk)
                destination.write(chunk)
        except ArtifactIntegrityError:
            raise
        except Exception as error:  # noqa: BLE001
            _LOGGER.error("Object storage download failed (%s).", type(error).__name__)
            raise ArtifactIntegrityError("ARTIFACT_STORAGE_UNAVAILABLE") from error
        finally:
            close = getattr(body, "close", None)

            if callable(close):
                close()

        if received != declared_size:
            raise ArtifactIntegrityError("ARTIFACT_SIZE_MISMATCH")

        if digest.hexdigest() != expected_sha256:
            raise ArtifactIntegrityError("ARTIFACT_HASH_MISMATCH")

    @contextmanager
    def verified_local_copy(
        self,
        artifact_uri: str,
        *,
        tenant_id: UUID,
        expected_sha256: str,
        filename: str,
        expected_size: Optional[int] = None,
    ) -> Iterator[Path]:
        key, expected = self._checked_key(artifact_uri, tenant_id, expected_sha256)
        directory = self._new_private_directory("ap-agent-source-")

        try:
            path = directory / safe_artifact_filename(filename)

            with path.open("wb") as destination:
                self._download(key, destination, expected_sha256=expected, expected_size=expected_size)

            os.chmod(path, 0o600)
            yield path
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def verify_integrity(
        self, artifact_uri: str, *, tenant_id: UUID, expected_sha256: str, expected_size: Optional[int] = None
    ) -> None:
        key, expected = self._checked_key(artifact_uri, tenant_id, expected_sha256)
        self._download(key, _NullSink(), expected_sha256=expected, expected_size=expected_size)  # type: ignore[arg-type]

    def open_verified(self, artifact_uri: str, *, tenant_id: UUID, expected_sha256: str) -> Path:
        raise ArtifactIntegrityError("ARTIFACT_STORE_HAS_NO_LOCAL_PATH")

    # -- cleanup ---------------------------------------------------------

    def delete(self, artifact_uri: str) -> None:
        """Roll back an uncommitted submission only. Never called by
        workflow processing."""

        try:
            tenant_hex, job_hex, sha = parse_object_uri(artifact_uri)
            self._client.delete_object(Bucket=self._bucket, Key=object_key_for(tenant_hex, job_hex, sha))
        except Exception as error:  # noqa: BLE001 - best-effort rollback
            _LOGGER.error("Object storage delete failed (%s).", type(error).__name__)


def _unavailable() -> OperationsRequestRejectedError:
    return OperationsRequestRejectedError("STORAGE_UNAVAILABLE", http_status=503)


def build_s3_client(config: S3StorageConfig) -> Any:
    """Create the boto3 client for an S3-compatible endpoint such as
    Cloudflare R2 (region `auto`, path-style addressing, checksums only
    where the request requires them). Imported lazily."""

    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        region_name=config.region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=config.connect_timeout_seconds,
            read_timeout=config.read_timeout_seconds,
            retries={"max_attempts": 3, "mode": "standard"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def build_aws_s3_client(config: AwsS3Config) -> Any:
    """Native Amazon S3 client (M11E.1). Credentials come from the default
    AWS chain -- on Lambda, the function's execution role -- never from the
    application's configuration. Regional virtual-hosted endpoint, SigV4,
    bounded timeouts. Imported lazily."""

    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=config.region,
        config=Config(
            signature_version="s3v4",
            connect_timeout=config.connect_timeout_seconds,
            read_timeout=config.read_timeout_seconds,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )

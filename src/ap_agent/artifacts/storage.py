"""Replaceable artifact storage for operations-console uploads.

New in M11D. `ArtifactStore` is the small interface the control plane and
the worker depend on; `LocalFilesystemArtifactStore` is the M11D
implementation. A later object-storage implementation only has to satisfy
the same four operations.

Security properties of the local implementation (all exercised in
`tests/unit/test_artifact_storage.py`):

  - the root is a server-side configuration value that is never returned to
    a client and never logged; the database stores only an opaque
    `artifact://<tenant>/<job>/<name>` *reference*;
  - storage paths are generated from UUIDs; the submitted filename is used
    only as the final, pre-validated component of the path;
  - tenants live in separate directories;
  - bytes are streamed to a staging file inside the root, hashed while
    received, size-capped, then atomically moved (`os.replace`) into place;
    a partial write is removed on any failure;
  - resolution re-validates the reference grammar, refuses `..`, refuses
    symlinked components and confirms the resolved path stays inside the
    root (traversal / symlink escape);
  - `open_verified` recomputes the SHA-256 and compares it with the recorded
    value before the worker may run any stage.

M11E adds a second implementation, `ap_agent.artifacts.s3.S3ArtifactStore`
(Cloudflare R2 / any S3-compatible service), behind the same interface. The
two methods the worker and resume executor use -- `verified_local_copy` and
`verify_integrity` -- exist on both: for the local store they simply verify
the in-place file; for the object store they download into a securely
created temporary directory that is always removed afterwards.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Optional, Protocol
from uuid import UUID

from ap_agent.exceptions import ArtifactIntegrityError, OperationsRequestRejectedError

__all__ = [
    "ARTIFACT_URI_PREFIX",
    "StagedArtifact",
    "ArtifactStore",
    "LocalFilesystemArtifactStore",
    "parse_artifact_uri",
    "spool_stream_to_file",
    "ARTIFACT_NAME_PATTERN",
    "safe_artifact_filename",
]

ARTIFACT_URI_PREFIX = "artifact://"

_URI_PATTERN = re.compile(
    r"^artifact://(?P<tenant>[0-9a-f]{32})/(?P<job>[0-9a-f]{32})/(?P<name>[\w][\w .()\-\[\]]{0,127})$",
    flags=re.UNICODE,
)

_CHUNK_BYTES = 64 * 1024
_EDGE_BYTES = 1024


ARTIFACT_NAME_PATTERN = re.compile(r"^[\w][\w .()\-\[\]]{0,127}$", flags=re.UNICODE)


def safe_artifact_filename(name: str) -> str:
    """The validated display name, or a fixed fallback; never a path."""

    return name if ARTIFACT_NAME_PATTERN.fullmatch(name) and ".." not in name else "source"


@dataclass(frozen=True)
class StagedArtifact:
    staging_path: Path
    sha256: str
    size: int
    head: bytes
    tail: bytes


class ArtifactStore(Protocol):
    def stage(self, tenant_id: UUID, stream: BinaryIO, *, maximum_bytes: int) -> StagedArtifact: ...

    def commit(self, staged: StagedArtifact, *, tenant_id: UUID, job_id: UUID, name: str) -> str: ...

    def discard(self, staged: StagedArtifact) -> None: ...

    def open_verified(self, artifact_uri: str, *, tenant_id: UUID, expected_sha256: str) -> Path: ...

    def verified_local_copy(
        self,
        artifact_uri: str,
        *,
        tenant_id: UUID,
        expected_sha256: str,
        filename: str,
        expected_size: Optional[int] = None,
    ) -> "AbstractContextManager[Path]": ...

    def verify_integrity(
        self, artifact_uri: str, *, tenant_id: UUID, expected_sha256: str, expected_size: Optional[int] = None
    ) -> None: ...

    def delete(self, artifact_uri: str) -> None: ...


def spool_stream_to_file(directory: Path, stream: BinaryIO, *, maximum_bytes: int) -> StagedArtifact:
    """Stream `stream` into a new private file in `directory`, hashing and
    size-capping while receiving (bounded memory: one chunk plus two small
    edge buffers). A partial file is removed on any failure."""

    digest = hashlib.sha256()
    size = 0
    head = b""
    tail = b""

    descriptor, staging_name = tempfile.mkstemp(dir=directory, prefix="upload-")
    staging_path = Path(staging_name)

    try:
        with os.fdopen(descriptor, "wb") as staging_file:
            while True:
                chunk = stream.read(_CHUNK_BYTES)

                if not chunk:
                    break

                size += len(chunk)

                if size > maximum_bytes:
                    raise OperationsRequestRejectedError("FILE_TOO_LARGE", http_status=413)

                if len(head) < _EDGE_BYTES:
                    head = (head + chunk)[:_EDGE_BYTES]

                tail = (tail + chunk)[-_EDGE_BYTES:]
                digest.update(chunk)
                staging_file.write(chunk)

            staging_file.flush()
            os.fsync(staging_file.fileno())
    except BaseException:
        staging_path.unlink(missing_ok=True)
        raise

    return StagedArtifact(staging_path=staging_path, sha256=digest.hexdigest(), size=size, head=head, tail=tail)


def parse_artifact_uri(artifact_uri: str) -> tuple[str, str, str]:
    match = _URI_PATTERN.fullmatch(artifact_uri)

    if match is None or ".." in match.group("name"):
        raise ArtifactIntegrityError("ARTIFACT_REFERENCE_INVALID")

    return match.group("tenant"), match.group("job"), match.group("name")


class LocalFilesystemArtifactStore:
    def __init__(self, root: Path) -> None:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)

        self._root = root.resolve(strict=True)

        if self._root.is_symlink():  # pragma: no cover - resolve() already follows it
            raise ArtifactIntegrityError("ARTIFACT_ROOT_INVALID")

    # -- staging ---------------------------------------------------------

    def _staging_directory(self, tenant_id: UUID) -> Path:
        directory = self._root / tenant_id.hex / ".staging"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def stage(self, tenant_id: UUID, stream: BinaryIO, *, maximum_bytes: int) -> StagedArtifact:
        return spool_stream_to_file(self._staging_directory(tenant_id), stream, maximum_bytes=maximum_bytes)

    def discard(self, staged: StagedArtifact) -> None: ...

    def open_verified(self, artifact_uri: str, *, tenant_id: UUID, expected_sha256: str) -> Path: ...

    def verified_local_copy(
        self,
        artifact_uri: str,
        *,
        tenant_id: UUID,
        expected_sha256: str,
        filename: str,
        expected_size: Optional[int] = None,
    ) -> "AbstractContextManager[Path]": ...

    def verify_integrity(
        self, artifact_uri: str, *, tenant_id: UUID, expected_sha256: str, expected_size: Optional[int] = None
    ) -> None: ...

    def delete(self, artifact_uri: str) -> None: ...


def parse_artifact_uri(artifact_uri: str) -> tuple[str, str, str]:
    match = _URI_PATTERN.fullmatch(artifact_uri)

    if match is None or ".." in match.group("name"):
        raise ArtifactIntegrityError("ARTIFACT_REFERENCE_INVALID")

    return match.group("tenant"), match.group("job"), match.group("name")


class LocalFilesystemArtifactStore:
    def __init__(self, root: Path) -> None:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)

        self._root = root.resolve(strict=True)

        if self._root.is_symlink():  # pragma: no cover - resolve() already follows it
            raise ArtifactIntegrityError("ARTIFACT_ROOT_INVALID")

    # -- staging ---------------------------------------------------------

    def _staging_directory(self, tenant_id: UUID) -> Path:
        directory = self._root / tenant_id.hex / ".staging"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def stage(self, tenant_id: UUID, stream: BinaryIO, *, maximum_bytes: int) -> StagedArtifact:
        digest = hashlib.sha256()
        size = 0
        head = b""
        tail = b""

        descriptor, staging_name = tempfile.mkstemp(dir=self._staging_directory(tenant_id), prefix="upload-")
        staging_path = Path(staging_name)

        try:
            with os.fdopen(descriptor, "wb") as staging_file:
                while True:
                    chunk = stream.read(_CHUNK_BYTES)

                    if not chunk:
                        break

                    size += len(chunk)

                    if size > maximum_bytes:
                        raise OperationsRequestRejectedError("FILE_TOO_LARGE", http_status=413)

                    if len(head) < _EDGE_BYTES:
                        head = (head + chunk)[:_EDGE_BYTES]

                    tail = (tail + chunk)[-_EDGE_BYTES:]
                    digest.update(chunk)
                    staging_file.write(chunk)

                staging_file.flush()
                os.fsync(staging_file.fileno())
        except BaseException:
            staging_path.unlink(missing_ok=True)
            raise

        return StagedArtifact(staging_path=staging_path, sha256=digest.hexdigest(), size=size, head=head, tail=tail)

    def discard(self, staged: StagedArtifact) -> None:
        staged.staging_path.unlink(missing_ok=True)

    def commit(self, staged: StagedArtifact, *, tenant_id: UUID, job_id: UUID, name: str) -> str:
        artifact_uri = f"{ARTIFACT_URI_PREFIX}{tenant_id.hex}/{job_id.hex}/{name}"
        parse_artifact_uri(artifact_uri)  # re-validates the name grammar

        destination = self._resolve(artifact_uri, must_exist=False)
        destination.parent.mkdir(parents=True, exist_ok=True)

        try:
            os.replace(staged.staging_path, destination)
        except BaseException:
            staged.staging_path.unlink(missing_ok=True)
            self._remove_empty_directory(destination.parent)
            raise

        return artifact_uri

    # -- resolution ------------------------------------------------------

    def _resolve(self, artifact_uri: str, *, must_exist: bool) -> Path:
        tenant_hex, job_hex, name = parse_artifact_uri(artifact_uri)

        candidate = self._root / tenant_hex / job_hex / name

        # Every existing component must be a real directory/file, never a link.
        probe = self._root

        for part in (tenant_hex, job_hex, name):
            probe = probe / part

            if probe.is_symlink():
                raise ArtifactIntegrityError("ARTIFACT_PATH_ESCAPE")

            if not probe.exists():
                break

        if must_exist and not candidate.is_file():
            raise ArtifactIntegrityError("ARTIFACT_MISSING")

        resolved_parent = candidate.parent.resolve(strict=False)

        if self._root != resolved_parent and self._root not in resolved_parent.parents:
            raise ArtifactIntegrityError("ARTIFACT_PATH_ESCAPE")

        return candidate

    def open_verified(self, artifact_uri: str, *, tenant_id: UUID, expected_sha256: str) -> Path:
        tenant_hex, _job_hex, _name = parse_artifact_uri(artifact_uri)

        if tenant_hex != tenant_id.hex:
            raise ArtifactIntegrityError("ARTIFACT_TENANT_MISMATCH")

        path = self._resolve(artifact_uri, must_exist=True)

        digest = hashlib.sha256()

        with path.open("rb") as artifact_file:
            for chunk in iter(lambda: artifact_file.read(_CHUNK_BYTES), b""):
                digest.update(chunk)

        if digest.hexdigest() != expected_sha256.strip().lower():
            raise ArtifactIntegrityError("ARTIFACT_HASH_MISMATCH")

        return path

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
        # The file already lives on the local filesystem: verify it in place
        # and never delete it (source invoices are immutable).
        yield self.open_verified(artifact_uri, tenant_id=tenant_id, expected_sha256=expected_sha256)

    def verify_integrity(
        self, artifact_uri: str, *, tenant_id: UUID, expected_sha256: str, expected_size: Optional[int] = None
    ) -> None:
        self.open_verified(artifact_uri, tenant_id=tenant_id, expected_sha256=expected_sha256)

    # -- cleanup ---------------------------------------------------------

    @staticmethod
    def _remove_empty_directory(directory: Path) -> None:
        try:
            directory.rmdir()
        except OSError:
            pass

    def delete(self, artifact_uri: str) -> None:
        try:
            path = self._resolve(artifact_uri, must_exist=False)
        except ArtifactIntegrityError:
            return

        path.unlink(missing_ok=True)
        self._remove_empty_directory(path.parent)

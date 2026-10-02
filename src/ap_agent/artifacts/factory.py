"""Builds the configured `ArtifactStore` (M11E). The API and the worker both
call this, so they can never disagree about the backend."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ap_agent.artifacts.storage import ArtifactStore
from ap_agent.config.deployment import S3StorageConfig, StorageMode

__all__ = ["build_artifact_store"]


def build_artifact_store(
    storage_mode: StorageMode,
    *,
    artifact_root: Optional[Path] = None,
    s3: Optional[S3StorageConfig] = None,
) -> ArtifactStore:
    if storage_mode is StorageMode.S3:
        assert s3 is not None, "S3 storage requires an S3 configuration."

        from ap_agent.artifacts.s3 import S3ArtifactStore, build_s3_client

        return S3ArtifactStore(build_s3_client(s3), s3.bucket)

    assert artifact_root is not None, "Local storage requires an artifact root."

    from ap_agent.artifacts.storage import LocalFilesystemArtifactStore

    return LocalFilesystemArtifactStore(artifact_root)

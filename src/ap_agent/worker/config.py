"""Worker configuration, read from the environment only when called.

New in M11D. Defines no module-level config instance (CLAUDE.md). The
worker refuses to execute anything unless `AP_AGENT_ENABLE_WORKER_EXECUTION`
is explicitly truthy: the default is fail-closed, independent of the API's
`AP_AGENT_ENABLE_OPERATIONS` and of the review-command write switch.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import UUID

from ap_agent.config.deployment import (
    AwsS3Config,
    DeploymentEnvironment,
    HostedConfigurationError,
    S3StorageConfig,
    StorageMode,
    hosted_configuration_problems,
    load_aws_s3_config,
    load_deployment_environment,
    load_s3_config,
    load_storage_mode,
)
from ap_agent.models.operations import WorkerSettings

__all__ = [
    "ENABLE_WORKER_EXECUTION_ENVIRONMENT_VARIABLE",
    "ARTIFACT_ROOT_ENVIRONMENT_VARIABLE",
    "PHASE_ARTIFACT_ROOT_ENVIRONMENT_VARIABLE",
    "REFERENCE_DATA_ENVIRONMENT_VARIABLE",
    "OCR_PROVIDER_ENVIRONMENT_VARIABLE",
    "TENANT_SCOPE_ENVIRONMENT_VARIABLE",
    "WorkerConfigurationError",
    "WorkerConfig",
    "load_worker_config",
]

ENABLE_WORKER_EXECUTION_ENVIRONMENT_VARIABLE = "AP_AGENT_ENABLE_WORKER_EXECUTION"
ARTIFACT_ROOT_ENVIRONMENT_VARIABLE = "AP_AGENT_ARTIFACT_ROOT"
PHASE_ARTIFACT_ROOT_ENVIRONMENT_VARIABLE = "AP_AGENT_PHASE_ARTIFACT_ROOT"
REFERENCE_DATA_ENVIRONMENT_VARIABLE = "AP_AGENT_REFERENCE_DATA_DIRECTORY"
OCR_PROVIDER_ENVIRONMENT_VARIABLE = "AP_AGENT_WORKER_OCR_PROVIDER"
TENANT_SCOPE_ENVIRONMENT_VARIABLE = "AP_AGENT_WORKER_TENANT_ID"

SUPPORTED_OCR_PROVIDERS = ("paddleocr", "tesseract")

_TRUTHY = {"1", "true", "yes", "on"}


class WorkerConfigurationError(Exception):
    """Invalid or missing worker configuration (message is path-free)."""


@dataclass(frozen=True)
class WorkerConfig:
    execution_enabled: bool
    # `None` when the worker reads uploads from object storage (M11E).
    artifact_root: Optional[Path]
    phase_artifact_root: Path
    reference_data_directory: Path
    ocr_provider: str = "paddleocr"
    # Optional: serve a single tenant only (tests/demo). Unset in production.
    tenant_scope: Optional[UUID] = None
    settings: WorkerSettings = field(default_factory=WorkerSettings)
    # M11E: deployment environment and object storage.
    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT
    storage_mode: StorageMode = StorageMode.LOCAL
    s3: Optional[S3StorageConfig] = None
    # M11E.1: native Amazon S3 through the execution role (no keys).
    aws_s3: Optional[AwsS3Config] = None

    @property
    def hosted(self) -> bool:
        return self.environment is DeploymentEnvironment.HOSTED


def load_worker_config(environment: dict[str, str] | None = None) -> WorkerConfig:
    env = os.environ if environment is None else environment

    enabled = env.get(ENABLE_WORKER_EXECUTION_ENVIRONMENT_VARIABLE, "").strip().lower() in _TRUTHY

    environment = load_deployment_environment(env)
    storage_mode = load_storage_mode(env)
    s3 = load_s3_config(env)
    aws_s3 = load_aws_s3_config(env) if storage_mode is StorageMode.AWS_S3 else None

    artifact_root_text = env.get(ARTIFACT_ROOT_ENVIRONMENT_VARIABLE, "").strip()
    reference_text = env.get(REFERENCE_DATA_ENVIRONMENT_VARIABLE, "").strip()

    problems = list(
        hosted_configuration_problems(
            environment=environment,
            auth_mode=None,
            clerk=None,
            storage_mode=storage_mode,
            s3=s3,
            artifact_root_configured=bool(artifact_root_text),
            operations_enabled=True,
            requires_storage=True,
            aws_s3=aws_s3,
        )
    )

    provider = env.get(OCR_PROVIDER_ENVIRONMENT_VARIABLE, "paddleocr").strip().lower() or "paddleocr"

    if environment is DeploymentEnvironment.HOSTED:
        # Hosted mode runs the intended real OCR provider, one tenant-agnostic worker.
        if provider != "paddleocr":
            problems.append("HOSTED_REQUIRES_OCR_PROVIDER_PADDLEOCR")

        if env.get(TENANT_SCOPE_ENVIRONMENT_VARIABLE, "").strip():
            problems.append("HOSTED_FORBIDS_WORKER_TENANT_SCOPE")

    if environment is DeploymentEnvironment.HOSTED:
        from ap_agent.config.deployment import dsn_separation_problems

        problems.extend(dsn_separation_problems(env))

    if problems:
        raise HostedConfigurationError(tuple(problems))

    if storage_mode is StorageMode.LOCAL and not artifact_root_text:
        raise WorkerConfigurationError(f"{ARTIFACT_ROOT_ENVIRONMENT_VARIABLE} is required.")

    if not reference_text:
        raise WorkerConfigurationError(f"{REFERENCE_DATA_ENVIRONMENT_VARIABLE} is required.")

    artifact_root = Path(artifact_root_text) if artifact_root_text else None
    phase_text = env.get(PHASE_ARTIFACT_ROOT_ENVIRONMENT_VARIABLE, "").strip()

    if phase_text:
        phase_root = Path(phase_text)
    elif artifact_root is not None:
        phase_root = artifact_root / ".phases"
    else:
        # Object-storage mode: per-job scratch space, removed after each job.
        phase_root = Path(tempfile.gettempdir()) / "ap-agent-phases"

    if provider not in SUPPORTED_OCR_PROVIDERS:
        raise WorkerConfigurationError(f"{OCR_PROVIDER_ENVIRONMENT_VARIABLE} must be one of {SUPPORTED_OCR_PROVIDERS}.")

    scope_text = env.get(TENANT_SCOPE_ENVIRONMENT_VARIABLE, "").strip()

    try:
        tenant_scope = UUID(scope_text) if scope_text else None
    except ValueError as error:
        raise WorkerConfigurationError(f"{TENANT_SCOPE_ENVIRONMENT_VARIABLE} must be a UUID.") from error

    return WorkerConfig(
        tenant_scope=tenant_scope,
        execution_enabled=enabled,
        artifact_root=artifact_root,
        phase_artifact_root=phase_root,
        reference_data_directory=Path(reference_text),
        ocr_provider=provider,
        environment=environment,
        storage_mode=storage_mode,
        s3=s3,
        aws_s3=aws_s3,
    )

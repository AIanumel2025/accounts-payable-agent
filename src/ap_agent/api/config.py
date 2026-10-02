"""M10 FastAPI application configuration.

New in M10 -- the notebook's Phase 9 Cell 9 hardcodes
`PHASE_9_ALLOW_COMMAND_COMMITS = False` as a module-level Python constant
inside the notebook (never a real deployment switch). Task §11 requires an
explicit, environment-driven configuration flag instead
(`AP_AGENT_ENABLE_REVIEW_COMMAND_WRITES`, default `false`), and task §13
requires configurable CORS with no permissive wildcard default. Per
CLAUDE.md, this module defines no module-level config *instance* -- every
caller gets a fresh `ApiConfig` from `load_api_config()`, reading the
environment only when that function is called (never at import time).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import UUID

from ap_agent.config.deployment import (
    AuthMode,
    AwsS3Config,
    ClerkConfig,
    DeploymentEnvironment,
    HostedConfigurationError,
    PlatformAuthMode,
    QueueBackend,
    S3StorageConfig,
    SqsDispatchConfig,
    StorageMode,
    hosted_configuration_problems,
    load_auth_mode,
    load_aws_s3_config,
    load_clerk_config,
    load_deployment_environment,
    load_platform_auth_mode,
    load_queue_backend,
    load_s3_config,
    load_sqs_config,
    load_storage_mode,
)
from ap_agent.models.operations import UploadLimits

__all__ = [
    "ENABLE_REVIEW_COMMAND_WRITES_ENVIRONMENT_VARIABLE",
    "CORS_ALLOWED_ORIGINS_ENVIRONMENT_VARIABLE",
    "ALLOWED_TENANT_IDS_ENVIRONMENT_VARIABLE",
    "ENABLE_OPERATIONS_ENVIRONMENT_VARIABLE",
    "ARTIFACT_ROOT_ENVIRONMENT_VARIABLE",
    "ApiConfig",
    "load_api_config",
]


ENABLE_REVIEW_COMMAND_WRITES_ENVIRONMENT_VARIABLE = "AP_AGENT_ENABLE_REVIEW_COMMAND_WRITES"
CORS_ALLOWED_ORIGINS_ENVIRONMENT_VARIABLE = "AP_AGENT_API_CORS_ORIGINS"
ALLOWED_TENANT_IDS_ENVIRONMENT_VARIABLE = "AP_AGENT_API_ALLOWED_TENANT_IDS"
# M11D Core: independent of the review-command write switch above; neither
# implies the other.
ENABLE_OPERATIONS_ENVIRONMENT_VARIABLE = "AP_AGENT_ENABLE_OPERATIONS"
ARTIFACT_ROOT_ENVIRONMENT_VARIABLE = "AP_AGENT_ARTIFACT_ROOT"

_TRUTHY_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ApiConfig:
    """Configuration for the Phase 9 review API, threaded explicitly into
    `ap_agent.api.app.create_app` -- never read as a hidden global by a
    route or service (CLAUDE.md)."""

    api_version: str = "v1"
    api_prefix: str = "/api/v1"

    # Task §11: defaults to `False` everywhere -- tests and docs must set
    # it explicitly, never globally, to exercise write-enabled behaviour.
    enable_review_command_writes: bool = False

    # Task §13: "configurable CORS with no permissive wildcard default."
    # Empty means no CORS middleware is installed at all (same-origin
    # only); a deployment opts in explicitly, one exact origin per entry,
    # never "*".
    cors_allow_origins: tuple[str, ...] = field(default_factory=tuple)

    request_timeout_seconds: float = 30.0

    # Empty (the default) means unrestricted multi-tenant service: any
    # tenant PostgreSQL's row-level security recognises may be served,
    # exactly like `ap_agent.repositories.postgres_memory_repository`.
    # A deployment scoped to specific tenant(s) -- including the
    # notebook's own single-prototype-tenant demo, reproduced by the M10
    # golden-parity tests -- lists them here explicitly instead of this
    # module hardcoding one (CLAUDE.md D-2/D-3); any other authenticated
    # tenant then fails closed with `TenantAccessDeniedError` (HTTP 403).
    allowed_tenant_ids: tuple[UUID, ...] = field(default_factory=tuple)

    # M11D Core: the operations console (upload + job queue reads). Default
    # `False`; enabling requires a server-side artifact root.
    enable_operations: bool = False
    artifact_root: Optional[Path] = None
    upload_limits: UploadLimits = field(default_factory=UploadLimits)

    # M11E: deployment environment, authentication mode and object storage.
    # Defaults reproduce the M11A-M11D behaviour (development, prototype
    # headers, local filesystem); `hosted` fails closed (see
    # `ap_agent.config.deployment.hosted_configuration_problems`).
    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT
    auth_mode: AuthMode = AuthMode.PROTOTYPE_HEADERS
    clerk: Optional[ClerkConfig] = None
    storage_mode: StorageMode = StorageMode.LOCAL
    s3: Optional[S3StorageConfig] = None

    # M11E.1 (AWS): native S3 via the execution role, how the platform
    # authenticates the caller before Clerk, and where accepted jobs are
    # dispatched. Defaults reproduce the Render/R2 behaviour exactly.
    aws_s3: Optional[AwsS3Config] = None
    platform_auth_mode: PlatformAuthMode = PlatformAuthMode.BEARER
    queue_backend: QueueBackend = QueueBackend.NONE
    sqs: Optional[SqsDispatchConfig] = None

    def __post_init__(self) -> None:
        assert self.api_version.strip()
        assert self.api_prefix.startswith("/")
        assert self.request_timeout_seconds > 0
        assert "*" not in self.cors_allow_origins, "CORS origins must not include a wildcard."
        if self.storage_mode is StorageMode.LOCAL:
            assert not self.enable_operations or self.artifact_root is not None, (
                "Enabling operations requires an artifact root."
            )

        problems = hosted_configuration_problems(
            environment=self.environment,
            auth_mode=self.auth_mode,
            clerk=self.clerk,
            storage_mode=self.storage_mode,
            s3=self.s3,
            artifact_root_configured=self.artifact_root is not None,
            operations_enabled=self.enable_operations,
            requires_storage=True,
            aws_s3=self.aws_s3,
            platform_auth_mode=self.platform_auth_mode,
            queue_backend=self.queue_backend,
            sqs=self.sqs,
        )

        if problems:
            raise HostedConfigurationError(problems)


def load_api_config() -> ApiConfig:
    """Build an `ApiConfig` from the environment. Called explicitly by
    `create_app`/tests -- never at module-import time."""

    raw_enable_writes = os.environ.get(ENABLE_REVIEW_COMMAND_WRITES_ENVIRONMENT_VARIABLE, "").strip().lower()
    enable_writes = raw_enable_writes in _TRUTHY_VALUES

    raw_origins = os.environ.get(CORS_ALLOWED_ORIGINS_ENVIRONMENT_VARIABLE, "").strip()
    origins = tuple(origin.strip() for origin in raw_origins.split(",") if origin.strip())

    raw_tenant_ids = os.environ.get(ALLOWED_TENANT_IDS_ENVIRONMENT_VARIABLE, "").strip()
    allowed_tenant_ids = tuple(
        UUID(value.strip()) for value in raw_tenant_ids.split(",") if value.strip()
    )

    enable_operations = (
        os.environ.get(ENABLE_OPERATIONS_ENVIRONMENT_VARIABLE, "").strip().lower() in _TRUTHY_VALUES
    )
    artifact_root_text = os.environ.get(ARTIFACT_ROOT_ENVIRONMENT_VARIABLE, "").strip()

    storage_mode = load_storage_mode()

    return ApiConfig(
        environment=load_deployment_environment(),
        auth_mode=load_auth_mode(),
        clerk=load_clerk_config(),
        storage_mode=storage_mode,
        s3=load_s3_config(),
        aws_s3=load_aws_s3_config() if storage_mode is StorageMode.AWS_S3 else None,
        platform_auth_mode=load_platform_auth_mode(),
        queue_backend=load_queue_backend(),
        sqs=load_sqs_config(),
        enable_operations=enable_operations,
        artifact_root=Path(artifact_root_text) if artifact_root_text else None,
        enable_review_command_writes=enable_writes,
        cors_allow_origins=origins,
        allowed_tenant_ids=allowed_tenant_ids,
    )

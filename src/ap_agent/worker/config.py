"""Worker configuration, read from the environment only when called.

New in M11D. Defines no module-level config instance (CLAUDE.md). The
worker refuses to execute anything unless `AP_AGENT_ENABLE_WORKER_EXECUTION`
is explicitly truthy: the default is fail-closed, independent of the API's
`AP_AGENT_ENABLE_OPERATIONS` and of the review-command write switch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import UUID

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
    artifact_root: Path
    phase_artifact_root: Path
    reference_data_directory: Path
    ocr_provider: str = "paddleocr"
    # Optional: serve a single tenant only (tests/demo). Unset in production.
    tenant_scope: Optional[UUID] = None
    settings: WorkerSettings = field(default_factory=WorkerSettings)


def load_worker_config(environment: dict[str, str] | None = None) -> WorkerConfig:
    env = os.environ if environment is None else environment

    enabled = env.get(ENABLE_WORKER_EXECUTION_ENVIRONMENT_VARIABLE, "").strip().lower() in _TRUTHY

    artifact_root_text = env.get(ARTIFACT_ROOT_ENVIRONMENT_VARIABLE, "").strip()
    reference_text = env.get(REFERENCE_DATA_ENVIRONMENT_VARIABLE, "").strip()

    if not artifact_root_text:
        raise WorkerConfigurationError(f"{ARTIFACT_ROOT_ENVIRONMENT_VARIABLE} is required.")

    if not reference_text:
        raise WorkerConfigurationError(f"{REFERENCE_DATA_ENVIRONMENT_VARIABLE} is required.")

    artifact_root = Path(artifact_root_text)
    phase_text = env.get(PHASE_ARTIFACT_ROOT_ENVIRONMENT_VARIABLE, "").strip()
    phase_root = Path(phase_text) if phase_text else artifact_root / ".phases"

    provider = env.get(OCR_PROVIDER_ENVIRONMENT_VARIABLE, "paddleocr").strip().lower() or "paddleocr"

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
    )

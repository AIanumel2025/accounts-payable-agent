"""Deployment configuration for the hosted MVP (M11E): authentication mode,
Clerk token-verification settings, object-storage settings and the
fail-closed hosted-startup validation shared by the API and the worker.

New in M11E (not sourced from the notebook). Per CLAUDE.md this module
defines no module-level config instance: every value is read from an
explicit environment mapping only when a `load_*` function is called.

Secrets (`S3StorageConfig.secret_access_key`, `.access_key_id`) are excluded
from `repr()`; `describe_configuration_status` reports only whether a named
variable is set, never its value.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional
from urllib.parse import urlsplit

__all__ = [
    "ENVIRONMENT_ENVIRONMENT_VARIABLE",
    "AUTH_MODE_ENVIRONMENT_VARIABLE",
    "ARTIFACT_STORAGE_ENVIRONMENT_VARIABLE",
    "DeploymentEnvironment",
    "AuthMode",
    "StorageMode",
    "ClerkConfig",
    "S3StorageConfig",
    "HostedConfigurationError",
    "load_deployment_environment",
    "load_auth_mode",
    "load_clerk_config",
    "load_storage_mode",
    "load_s3_config",
    "hosted_configuration_problems",
    "describe_configuration_status",
    "dsn_separation_problems",
]

ENVIRONMENT_ENVIRONMENT_VARIABLE = "AP_AGENT_ENVIRONMENT"
AUTH_MODE_ENVIRONMENT_VARIABLE = "AP_AGENT_AUTH_MODE"
ARTIFACT_STORAGE_ENVIRONMENT_VARIABLE = "AP_AGENT_ARTIFACT_STORAGE"

CLERK_ISSUER_ENVIRONMENT_VARIABLE = "AP_AGENT_CLERK_ISSUER"
CLERK_JWKS_URL_ENVIRONMENT_VARIABLE = "AP_AGENT_CLERK_JWKS_URL"
CLERK_AUTHORIZED_PARTIES_ENVIRONMENT_VARIABLE = "AP_AGENT_CLERK_AUTHORIZED_PARTIES"
CLERK_AUDIENCE_ENVIRONMENT_VARIABLE = "AP_AGENT_CLERK_AUDIENCE"

S3_ENDPOINT_ENVIRONMENT_VARIABLE = "AP_AGENT_S3_ENDPOINT_URL"
S3_BUCKET_ENVIRONMENT_VARIABLE = "AP_AGENT_S3_BUCKET"
S3_ACCESS_KEY_ID_ENVIRONMENT_VARIABLE = "AP_AGENT_S3_ACCESS_KEY_ID"
S3_SECRET_ACCESS_KEY_ENVIRONMENT_VARIABLE = "AP_AGENT_S3_SECRET_ACCESS_KEY"
S3_REGION_ENVIRONMENT_VARIABLE = "AP_AGENT_S3_REGION"

ARTIFACT_ROOT_ENVIRONMENT_VARIABLE = "AP_AGENT_ARTIFACT_ROOT"

_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION_PATTERN = re.compile(r"^[a-z0-9-]{2,32}$")


class DeploymentEnvironment(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    HOSTED = "hosted"


class AuthMode(str, Enum):
    PROTOTYPE_HEADERS = "prototype_headers"
    CLERK_JWT = "clerk_jwt"


class StorageMode(str, Enum):
    LOCAL = "local"
    S3 = "s3"


class HostedConfigurationError(Exception):
    """Startup refused: the configuration is unsafe or inconsistent for the
    selected deployment environment. `problems` holds stable codes that name
    variables or settings -- never values."""

    def __init__(self, problems: tuple[str, ...]):
        super().__init__("Invalid deployment configuration: " + ", ".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True)
class ClerkConfig:
    issuer: str
    jwks_url: str
    authorized_parties: tuple[str, ...] = field(default_factory=tuple)
    audience: Optional[str] = None
    leeway_seconds: int = 5
    jwks_cache_seconds: int = 3600
    jwks_minimum_refresh_seconds: int = 30
    jwks_timeout_seconds: float = 5.0


@dataclass(frozen=True)
class S3StorageConfig:
    endpoint_url: str
    bucket: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    region: str = "auto"
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0


def _environment(source: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if source is None else source


def load_deployment_environment(source: Optional[Mapping[str, str]] = None) -> DeploymentEnvironment:
    raw = _environment(source).get(ENVIRONMENT_ENVIRONMENT_VARIABLE, "").strip().lower()

    if not raw:
        return DeploymentEnvironment.DEVELOPMENT

    try:
        return DeploymentEnvironment(raw)
    except ValueError as error:
        raise HostedConfigurationError((f"{ENVIRONMENT_ENVIRONMENT_VARIABLE}_INVALID",)) from error


def load_auth_mode(source: Optional[Mapping[str, str]] = None) -> AuthMode:
    raw = _environment(source).get(AUTH_MODE_ENVIRONMENT_VARIABLE, "").strip().lower()

    if not raw:
        return AuthMode.PROTOTYPE_HEADERS

    try:
        return AuthMode(raw)
    except ValueError as error:
        raise HostedConfigurationError((f"{AUTH_MODE_ENVIRONMENT_VARIABLE}_INVALID",)) from error


def load_storage_mode(source: Optional[Mapping[str, str]] = None) -> StorageMode:
    raw = _environment(source).get(ARTIFACT_STORAGE_ENVIRONMENT_VARIABLE, "").strip().lower()

    if not raw:
        return StorageMode.LOCAL

    try:
        return StorageMode(raw)
    except ValueError as error:
        raise HostedConfigurationError((f"{ARTIFACT_STORAGE_ENVIRONMENT_VARIABLE}_INVALID",)) from error


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def load_clerk_config(source: Optional[Mapping[str, str]] = None) -> Optional[ClerkConfig]:
    """Returns `None` when no issuer is configured. Syntax problems are
    reported by `hosted_configuration_problems`, not raised here, so the
    caller sees every problem at once."""

    env = _environment(source)
    issuer = env.get(CLERK_ISSUER_ENVIRONMENT_VARIABLE, "").strip().rstrip("/")

    if not issuer:
        return None

    jwks_url = env.get(CLERK_JWKS_URL_ENVIRONMENT_VARIABLE, "").strip() or f"{issuer}/.well-known/jwks.json"
    audience = env.get(CLERK_AUDIENCE_ENVIRONMENT_VARIABLE, "").strip() or None

    return ClerkConfig(
        issuer=issuer,
        jwks_url=jwks_url,
        authorized_parties=_csv(env.get(CLERK_AUTHORIZED_PARTIES_ENVIRONMENT_VARIABLE, "")),
        audience=audience,
    )


def load_s3_config(source: Optional[Mapping[str, str]] = None) -> Optional[S3StorageConfig]:
    """Returns `None` unless every required value is present."""

    env = _environment(source)
    endpoint = env.get(S3_ENDPOINT_ENVIRONMENT_VARIABLE, "").strip().rstrip("/")
    bucket = env.get(S3_BUCKET_ENVIRONMENT_VARIABLE, "").strip()
    access_key_id = env.get(S3_ACCESS_KEY_ID_ENVIRONMENT_VARIABLE, "").strip()
    secret_access_key = env.get(S3_SECRET_ACCESS_KEY_ENVIRONMENT_VARIABLE, "").strip()
    region = env.get(S3_REGION_ENVIRONMENT_VARIABLE, "").strip() or "auto"

    if not (endpoint and bucket and access_key_id and secret_access_key):
        return None

    return S3StorageConfig(
        endpoint_url=endpoint,
        bucket=bucket,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        region=region,
    )


def _is_origin(value: str, *, require_https: bool) -> bool:
    parts = urlsplit(value)

    if parts.scheme not in {"https", "http"} or (require_https and parts.scheme != "https"):
        return False

    return bool(parts.hostname) and parts.path in {"", "/"} and not parts.query and not parts.fragment


def _is_https_url(value: str, *, require_https: bool) -> bool:
    parts = urlsplit(value)

    if parts.scheme not in {"https", "http"} or (require_https and parts.scheme != "https"):
        return False

    return bool(parts.hostname) and not parts.query and not parts.fragment and not parts.username


def hosted_configuration_problems(
    *,
    environment: DeploymentEnvironment,
    auth_mode: Optional[AuthMode],
    clerk: Optional[ClerkConfig],
    storage_mode: StorageMode,
    s3: Optional[S3StorageConfig],
    artifact_root_configured: bool,
    operations_enabled: bool,
    requires_storage: bool,
) -> tuple[str, ...]:
    """Every reason the configuration is unusable. Empty means acceptable.

    Syntax checks on Clerk and S3 values apply in every environment whenever
    that mode is selected; the strict requirements (no prototype headers, no
    local filesystem, https only, operations on) apply to `hosted`.
    `auth_mode=None` means the process authenticates nobody (the worker), so
    only the storage rules apply.
    """

    hosted = environment is DeploymentEnvironment.HOSTED
    problems: list[str] = []

    if hosted and auth_mode is not None and auth_mode is not AuthMode.CLERK_JWT:
        problems.append("HOSTED_REQUIRES_AUTH_MODE_CLERK_JWT")

    if auth_mode is AuthMode.CLERK_JWT:
        if clerk is None:
            problems.append("CLERK_ISSUER_MISSING")
        else:
            if not _is_https_url(clerk.issuer, require_https=hosted):
                problems.append("CLERK_ISSUER_MALFORMED")

            if not _is_https_url(clerk.jwks_url, require_https=hosted):
                problems.append("CLERK_JWKS_URL_MALFORMED")

            if not clerk.authorized_parties:
                problems.append("CLERK_AUTHORIZED_PARTIES_MISSING")
            elif not all(_is_origin(party, require_https=hosted) for party in clerk.authorized_parties):
                problems.append("CLERK_AUTHORIZED_PARTIES_MALFORMED")

    if hosted:
        if storage_mode is not StorageMode.S3:
            problems.append("HOSTED_REQUIRES_OBJECT_STORAGE")

        if artifact_root_configured:
            problems.append("HOSTED_FORBIDS_LOCAL_ARTIFACT_ROOT")

        if requires_storage and not operations_enabled:
            problems.append("HOSTED_REQUIRES_OPERATIONS_ENABLED")

    if storage_mode is StorageMode.S3:
        if s3 is None:
            problems.append("S3_CONFIGURATION_INCOMPLETE")
        else:
            if not _is_https_url(s3.endpoint_url, require_https=hosted):
                problems.append("S3_ENDPOINT_MALFORMED")

            if not _BUCKET_PATTERN.fullmatch(s3.bucket):
                problems.append("S3_BUCKET_MALFORMED")

            if not _REGION_PATTERN.fullmatch(s3.region):
                problems.append("S3_REGION_MALFORMED")

    return tuple(problems)


_STATUS_VARIABLES = (
    CLERK_ISSUER_ENVIRONMENT_VARIABLE,
    CLERK_JWKS_URL_ENVIRONMENT_VARIABLE,
    CLERK_AUTHORIZED_PARTIES_ENVIRONMENT_VARIABLE,
    S3_ENDPOINT_ENVIRONMENT_VARIABLE,
    S3_BUCKET_ENVIRONMENT_VARIABLE,
    S3_ACCESS_KEY_ID_ENVIRONMENT_VARIABLE,
    S3_SECRET_ACCESS_KEY_ENVIRONMENT_VARIABLE,
    S3_REGION_ENVIRONMENT_VARIABLE,
    ARTIFACT_ROOT_ENVIRONMENT_VARIABLE,
    "AP_AGENT_POSTGRES_DSN",
)


def describe_configuration_status(source: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Safe startup diagnostics: names plus `set`/`unset`, and the three
    non-secret mode switches' values. Never a credential, DSN, hostname or
    identifier value."""

    env = _environment(source)
    status = {name: ("set" if env.get(name, "").strip() else "unset") for name in _STATUS_VARIABLES}

    def _mode(name: str, default: str, allowed: tuple[str, ...]) -> str:
        value = env.get(name, "").strip().lower() or default
        return value if value in allowed else "invalid"

    status[ENVIRONMENT_ENVIRONMENT_VARIABLE] = _mode(
        ENVIRONMENT_ENVIRONMENT_VARIABLE, "development", tuple(item.value for item in DeploymentEnvironment)
    )
    status[AUTH_MODE_ENVIRONMENT_VARIABLE] = _mode(
        AUTH_MODE_ENVIRONMENT_VARIABLE, "prototype_headers", tuple(item.value for item in AuthMode)
    )
    status[ARTIFACT_STORAGE_ENVIRONMENT_VARIABLE] = _mode(
        ARTIFACT_STORAGE_ENVIRONMENT_VARIABLE, "local", tuple(item.value for item in StorageMode)
    )

    return status


def dsn_separation_problems(source: Optional[Mapping[str, str]] = None) -> tuple[str, ...]:
    """The runtime and migration/admin credentials must differ: a service that
    serves requests never silently runs as the schema owner."""

    env = _environment(source)
    runtime = env.get("AP_AGENT_POSTGRES_DSN", "").strip()
    migration = env.get("AP_AGENT_POSTGRES_MIGRATION_DSN", "").strip()

    if runtime and migration and runtime == migration:
        return ("RUNTIME_DSN_EQUALS_MIGRATION_DSN",)

    return ()

"""Runtime retrieval of secrets from SSM Parameter Store (M11E.1).

Secrets never travel through CloudFormation, image layers, build arguments or
Lambda environment variables. The function's environment holds only *names*:

    AP_AGENT_SSM_PARAMETERS=AP_AGENT_POSTGRES_DSN=/ap-agent/production/postgres-runtime-dsn,...

At start-up (cold start) this module reads each listed SecureString with
decryption and places the value in `os.environ` under the given variable name,
unless that variable is already set. The execution role allows `ssm:GetParameter`
on exactly those parameter ARNs. Values are never logged; failures name the
variable, never the value or the parameter path.

`boto3` is imported lazily, only when parameters are listed.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, MutableMapping, Optional

__all__ = [
    "SSM_PARAMETERS_ENVIRONMENT_VARIABLE",
    "SecretLoadError",
    "parse_parameter_mapping",
    "load_ssm_parameters_into_environment",
]

SSM_PARAMETERS_ENVIRONMENT_VARIABLE = "AP_AGENT_SSM_PARAMETERS"

_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,80}$")
_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9_.\-/]{1,1000}$")


class SecretLoadError(RuntimeError):
    """A configured secret could not be loaded. The message names the
    environment variable only."""


def parse_parameter_mapping(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}

    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue

        name, separator, path = item.partition("=")

        if not separator or not _NAME_PATTERN.fullmatch(name) or not _PATH_PATTERN.fullmatch(path):
            raise SecretLoadError(f"{SSM_PARAMETERS_ENVIRONMENT_VARIABLE} is malformed.")

        mapping[name] = path

    return mapping


def load_ssm_parameters_into_environment(
    environment: Optional[MutableMapping[str, str]] = None, *, client: Optional[Any] = None
) -> tuple[str, ...]:
    """Returns the names of the variables that were loaded."""

    env: MutableMapping[str, str] = os.environ if environment is None else environment
    mapping = parse_parameter_mapping(env.get(SSM_PARAMETERS_ENVIRONMENT_VARIABLE, ""))
    missing = {name: path for name, path in mapping.items() if not env.get(name, "").strip()}

    if not missing:
        return ()

    if client is None:
        import boto3

        client = boto3.client("ssm", region_name=env.get("AWS_REGION") or None)

    loaded: list[str] = []

    for name, path in missing.items():
        try:
            value = client.get_parameter(Name=path, WithDecryption=True)["Parameter"]["Value"]
        except Exception as error:  # noqa: BLE001 - never leak the path or the message
            raise SecretLoadError(f"{name} could not be loaded from the secret store ({type(error).__name__}).") from error

        if not isinstance(value, str) or not value.strip():
            raise SecretLoadError(f"{name} is empty in the secret store.")

        env[name] = value.strip()
        loaded.append(name)

    return tuple(loaded)

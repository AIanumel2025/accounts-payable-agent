"""M11E: fail-closed hosted configuration and safe diagnostics."""

from __future__ import annotations

import pytest

from ap_agent.api.config import ApiConfig, load_api_config
from ap_agent.config.deployment import (
    AuthMode,
    DeploymentEnvironment,
    HostedConfigurationError,
    S3StorageConfig,
    StorageMode,
    describe_configuration_status,
    load_s3_config,
)
from tests.support.m11e_auth import clerk_config

pytestmark = pytest.mark.unit

HOSTED_ENV = {
    "AP_AGENT_ENVIRONMENT": "hosted",
    "AP_AGENT_AUTH_MODE": "clerk_jwt",
    "AP_AGENT_CLERK_ISSUER": "https://clerk.example.test",
    "AP_AGENT_CLERK_AUTHORIZED_PARTIES": "https://app.example.test",
    "AP_AGENT_ARTIFACT_STORAGE": "s3",
    "AP_AGENT_S3_ENDPOINT_URL": "https://objects.example.test",
    "AP_AGENT_S3_BUCKET": "example-bucket",
    "AP_AGENT_S3_ACCESS_KEY_ID": "AKIAEXAMPLEEXAMPLE",
    "AP_AGENT_S3_SECRET_ACCESS_KEY": "super-secret-value",
    "AP_AGENT_ENABLE_OPERATIONS": "true",
}


def _load(monkeypatch, **overrides):
    for name in list(HOSTED_ENV) + ["AP_AGENT_ARTIFACT_ROOT", "AP_AGENT_S3_REGION"]:
        monkeypatch.delenv(name, raising=False)

    values = {**HOSTED_ENV, **overrides}

    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    return load_api_config()


def _problems(monkeypatch, **overrides) -> tuple[str, ...]:
    with pytest.raises(HostedConfigurationError) as caught:
        _load(monkeypatch, **overrides)

    return caught.value.problems


def test_complete_hosted_configuration_loads(monkeypatch):
    config = _load(monkeypatch)
    assert config.environment is DeploymentEnvironment.HOSTED
    assert config.auth_mode is AuthMode.CLERK_JWT
    assert config.storage_mode is StorageMode.S3
    assert config.s3 is not None and config.s3.region == "auto"


def test_hosted_refuses_prototype_authentication(monkeypatch):
    assert "HOSTED_REQUIRES_AUTH_MODE_CLERK_JWT" in _problems(monkeypatch, AP_AGENT_AUTH_MODE="prototype_headers")
    assert "HOSTED_REQUIRES_AUTH_MODE_CLERK_JWT" in _problems(monkeypatch, AP_AGENT_AUTH_MODE=None)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"AP_AGENT_CLERK_ISSUER": None}, "CLERK_ISSUER_MISSING"),
        ({"AP_AGENT_CLERK_ISSUER": "http://clerk.example.test"}, "CLERK_ISSUER_MALFORMED"),
        ({"AP_AGENT_CLERK_ISSUER": "not a url"}, "CLERK_ISSUER_MALFORMED"),
        ({"AP_AGENT_CLERK_AUTHORIZED_PARTIES": None}, "CLERK_AUTHORIZED_PARTIES_MISSING"),
        ({"AP_AGENT_CLERK_AUTHORIZED_PARTIES": "https://app.example.test/path"}, "CLERK_AUTHORIZED_PARTIES_MALFORMED"),
        ({"AP_AGENT_CLERK_AUTHORIZED_PARTIES": "http://app.example.test"}, "CLERK_AUTHORIZED_PARTIES_MALFORMED"),
    ],
)
def test_hosted_refuses_absent_or_malformed_clerk_configuration(monkeypatch, overrides, expected):
    assert expected in _problems(monkeypatch, **overrides)


def test_hosted_refuses_local_filesystem_storage(monkeypatch):
    problems = _problems(monkeypatch, AP_AGENT_ARTIFACT_STORAGE="local", AP_AGENT_ARTIFACT_ROOT="/tmp/artifacts")
    assert "HOSTED_REQUIRES_OBJECT_STORAGE" in problems
    assert "HOSTED_FORBIDS_LOCAL_ARTIFACT_ROOT" in problems


def test_hosted_refuses_a_leftover_artifact_root_even_with_object_storage(monkeypatch):
    assert "HOSTED_FORBIDS_LOCAL_ARTIFACT_ROOT" in _problems(monkeypatch, AP_AGENT_ARTIFACT_ROOT="/tmp/artifacts")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"AP_AGENT_S3_BUCKET": None}, "S3_CONFIGURATION_INCOMPLETE"),
        ({"AP_AGENT_S3_SECRET_ACCESS_KEY": None}, "S3_CONFIGURATION_INCOMPLETE"),
        ({"AP_AGENT_S3_ENDPOINT_URL": "http://objects.example.test"}, "S3_ENDPOINT_MALFORMED"),
        ({"AP_AGENT_S3_BUCKET": "Bad_Bucket!"}, "S3_BUCKET_MALFORMED"),
    ],
)
def test_hosted_refuses_incomplete_or_malformed_object_storage(monkeypatch, overrides, expected):
    assert expected in _problems(monkeypatch, **overrides)


def test_hosted_requires_operations_to_be_enabled(monkeypatch):
    assert "HOSTED_REQUIRES_OPERATIONS_ENABLED" in _problems(monkeypatch, AP_AGENT_ENABLE_OPERATIONS=None)


def test_invalid_mode_values_are_refused(monkeypatch):
    assert _problems(monkeypatch, AP_AGENT_ENVIRONMENT="staging") == ("AP_AGENT_ENVIRONMENT_INVALID",)


def test_development_defaults_are_unchanged():
    config = ApiConfig()
    assert config.environment is DeploymentEnvironment.DEVELOPMENT
    assert config.auth_mode is AuthMode.PROTOTYPE_HEADERS
    assert config.storage_mode is StorageMode.LOCAL


def test_test_environment_may_use_http_endpoints_and_local_values():
    ApiConfig(
        environment=DeploymentEnvironment.TEST,
        auth_mode=AuthMode.CLERK_JWT,
        clerk=clerk_config(issuer="http://127.0.0.1:1", jwks_url="http://127.0.0.1:1/jwks", authorized_parties=("http://127.0.0.1:3000",)),
        storage_mode=StorageMode.S3,
        s3=S3StorageConfig(endpoint_url="http://127.0.0.1:5000", bucket="test-bucket", access_key_id="a", secret_access_key="b"),
        enable_operations=True,
    )


def test_error_and_repr_never_contain_secret_values(monkeypatch):
    config = _load(monkeypatch)
    assert "super-secret-value" not in repr(config)
    assert "AKIAEXAMPLEEXAMPLE" not in repr(config)

    problems = _problems(monkeypatch, AP_AGENT_S3_ENDPOINT_URL="http://objects.example.test")
    assert "super-secret-value" not in " ".join(problems)


def test_diagnostics_report_names_and_status_only():
    status = describe_configuration_status({**HOSTED_ENV, "AP_AGENT_POSTGRES_DSN": "postgresql://user:pw@host/db"})
    assert status["AP_AGENT_POSTGRES_DSN"] == "set"
    assert status["AP_AGENT_S3_SECRET_ACCESS_KEY"] == "set"
    assert status["AP_AGENT_ARTIFACT_ROOT"] == "unset"
    assert status["AP_AGENT_AUTH_MODE"] == "clerk_jwt"
    rendered = repr(status)

    for secret in ("pw", "super-secret-value", "AKIAEXAMPLEEXAMPLE", "objects.example.test", "example-bucket", "clerk.example.test"):
        assert secret not in rendered


def test_diagnostics_do_not_echo_arbitrary_mode_values():
    status = describe_configuration_status({"AP_AGENT_AUTH_MODE": "my-secret-token"})
    assert status["AP_AGENT_AUTH_MODE"] == "invalid"


def test_load_s3_config_requires_every_value():
    assert load_s3_config({"AP_AGENT_S3_BUCKET": "b"}) is None

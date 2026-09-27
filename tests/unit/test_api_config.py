"""M10 unit tests: `ap_agent.api.config` -- write-mode default, CORS
wildcard rejection, environment parsing (task §11/§13)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from ap_agent.api.config import (
    ALLOWED_TENANT_IDS_ENVIRONMENT_VARIABLE,
    CORS_ALLOWED_ORIGINS_ENVIRONMENT_VARIABLE,
    ENABLE_REVIEW_COMMAND_WRITES_ENVIRONMENT_VARIABLE,
    ApiConfig,
    load_api_config,
)

pytestmark = pytest.mark.unit


def test_write_mode_defaults_to_false():
    config = ApiConfig()
    assert config.enable_review_command_writes is False


def test_load_api_config_defaults_to_false_when_env_var_unset(monkeypatch):
    monkeypatch.delenv(ENABLE_REVIEW_COMMAND_WRITES_ENVIRONMENT_VARIABLE, raising=False)
    config = load_api_config()

    assert config.enable_review_command_writes is False


@pytest.mark.parametrize("raw_value", ["true", "1", "yes", "on", "TRUE"])
def test_load_api_config_recognizes_truthy_values(monkeypatch, raw_value):
    monkeypatch.setenv(ENABLE_REVIEW_COMMAND_WRITES_ENVIRONMENT_VARIABLE, raw_value)
    config = load_api_config()

    assert config.enable_review_command_writes is True


@pytest.mark.parametrize("raw_value", ["false", "0", "", "no", "garbage"])
def test_load_api_config_treats_everything_else_as_false(monkeypatch, raw_value):
    monkeypatch.setenv(ENABLE_REVIEW_COMMAND_WRITES_ENVIRONMENT_VARIABLE, raw_value)
    config = load_api_config()

    assert config.enable_review_command_writes is False


def test_cors_wildcard_is_rejected():
    with pytest.raises(AssertionError):
        ApiConfig(cors_allow_origins=("*",))


def test_cors_defaults_to_no_origins():
    config = ApiConfig()
    assert config.cors_allow_origins == tuple()


def test_load_api_config_parses_comma_separated_origins(monkeypatch):
    monkeypatch.setenv(CORS_ALLOWED_ORIGINS_ENVIRONMENT_VARIABLE, "https://a.example.com, https://b.example.com")
    config = load_api_config()

    assert config.cors_allow_origins == ("https://a.example.com", "https://b.example.com")


def test_load_api_config_parses_allowed_tenant_ids(monkeypatch):
    tenant_a, tenant_b = uuid4(), uuid4()
    monkeypatch.setenv(ALLOWED_TENANT_IDS_ENVIRONMENT_VARIABLE, f"{tenant_a},{tenant_b}")
    config = load_api_config()

    assert config.allowed_tenant_ids == (tenant_a, tenant_b)


def test_allowed_tenant_ids_defaults_to_unrestricted(monkeypatch):
    monkeypatch.delenv(ALLOWED_TENANT_IDS_ENVIRONMENT_VARIABLE, raising=False)
    config = load_api_config()

    assert config.allowed_tenant_ids == tuple()

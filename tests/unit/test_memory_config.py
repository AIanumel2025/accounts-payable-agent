"""M8D unit tests: PostgreSQL configuration validation and DSN redaction."""

from __future__ import annotations

import pytest

from ap_agent.config.postgres import (
    MemoryConfig,
    PostgresPoolConfig,
    PostgresTransportPolicy,
)
from ap_agent.db.connection import (
    dsn_is_configured,
    inspect_transport,
    load_dsn,
    redact_dsn,
    verify_transport_policy,
)
from ap_agent.exceptions import PostgresConfigurationError

pytestmark = pytest.mark.unit


def test_memory_config_defaults_are_safe_for_disposable_postgres():
    config = MemoryConfig()

    assert config.schema_name == "ap_agent"
    assert config.dsn_environment_variable == "AP_AGENT_POSTGRES_DSN"
    assert config.migration_dsn_environment_variable == "AP_AGENT_POSTGRES_MIGRATION_DSN"
    assert config.allow_migration_dsn_fallback is False
    assert config.transport_policy.require_ssl is True
    assert config.transport_policy.require_channel_binding is False
    assert config.transport_policy.require_pooled_endpoint is False
    assert config.enforce_tenant_isolation is True
    assert config.append_only_audit is True


def test_memory_config_no_default_tenant_id_attribute():
    """Task §3.3: no production dependence on a prototype tenant constant."""

    config = MemoryConfig()

    assert not hasattr(config, "default_tenant_id")


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("schema_name", "Not-Valid"),
        ("connect_timeout_seconds", 0),
        ("statement_timeout_milliseconds", 0),
        ("lock_timeout_milliseconds", 0),
        ("application_name", "   "),
    ],
)
def test_memory_config_rejects_invalid_values(field_name, value):
    with pytest.raises(AssertionError):
        MemoryConfig(**{field_name: value})


def test_memory_config_rejects_pool_maximum_below_minimum():
    with pytest.raises(AssertionError):
        MemoryConfig(pool=PostgresPoolConfig(minimum_size=5, maximum_size=1))


def test_redact_dsn_masks_password_only():
    dsn = "postgresql://appuser:s3cret@db.example.com:5432/ap_agent?sslmode=require"
    redacted = redact_dsn(dsn)

    assert "s3cret" not in redacted
    assert "appuser" in redacted
    assert "db.example.com" in redacted
    assert "ap_agent" in redacted


def test_redact_dsn_is_a_noop_for_empty_string():
    assert redact_dsn("") == ""


def test_dsn_is_configured_false_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("AP_AGENT_POSTGRES_DSN", raising=False)

    config = MemoryConfig()

    assert dsn_is_configured(config) is False


def test_load_dsn_raises_without_masking_message_when_missing(monkeypatch):
    monkeypatch.delenv("AP_AGENT_POSTGRES_DSN", raising=False)

    config = MemoryConfig()

    with pytest.raises(PostgresConfigurationError):
        load_dsn(config)


def test_load_dsn_rejects_non_postgres_scheme(monkeypatch):
    monkeypatch.setenv("AP_AGENT_POSTGRES_DSN", "mysql://user:pass@host/db")

    config = MemoryConfig()

    with pytest.raises(PostgresConfigurationError):
        load_dsn(config)


def test_load_dsn_reads_configured_value(monkeypatch):
    monkeypatch.setenv("AP_AGENT_POSTGRES_DSN", "postgresql://user:pass@host/db")

    config = MemoryConfig()

    assert load_dsn(config) == "postgresql://user:pass@host/db"


def test_load_dsn_migration_fallback_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("AP_AGENT_POSTGRES_MIGRATION_DSN", raising=False)
    monkeypatch.setenv("AP_AGENT_POSTGRES_DSN", "postgresql://user:pass@host/db")

    config = MemoryConfig(allow_migration_dsn_fallback=False)

    with pytest.raises(PostgresConfigurationError):
        load_dsn(config, for_migration=True)

    config_with_fallback = MemoryConfig(allow_migration_dsn_fallback=True)

    assert (
        load_dsn(config_with_fallback, for_migration=True)
        == "postgresql://user:pass@host/db"
    )


def test_inspect_transport_reports_ssl_and_channel_binding():
    dsn = "postgresql://user:pass@ep-example-pooler.neon.tech/db?sslmode=require&channel_binding=require"
    transport = inspect_transport(dsn)

    assert transport["ssl_enforced"] is True
    assert transport["channel_binding_enforced"] is True
    assert transport["pooled_endpoint"] is True


def test_inspect_transport_local_postgres_is_not_pooled():
    transport = inspect_transport("postgresql://user:pass@localhost:5432/ap_agent")

    assert transport["pooled_endpoint"] is False
    assert transport["channel_binding_enforced"] is False


def test_verify_transport_policy_rejects_missing_ssl_when_required():
    with pytest.raises(PostgresConfigurationError):
        verify_transport_policy(
            "postgresql://user:pass@localhost/db",
            PostgresTransportPolicy(require_ssl=True),
        )


def test_verify_transport_policy_allows_plain_local_dsn_with_default_local_policy():
    # Disposable local PostgreSQL: SSL not required by default policy.
    transport = verify_transport_policy(
        "postgresql://user:pass@localhost/db",
        PostgresTransportPolicy(require_ssl=False),
    )

    assert transport["database"] == "db"


def test_verify_transport_policy_rejects_missing_channel_binding_when_required():
    with pytest.raises(PostgresConfigurationError):
        verify_transport_policy(
            "postgresql://user:pass@ep-example-pooler.neon.tech/db?sslmode=require",
            PostgresTransportPolicy(require_ssl=True, require_channel_binding=True),
        )

"""M11E.3: the pooled-endpoint connection path against a REAL PostgreSQL (RLS, read-only transactions, no leakage).

A Neon transaction-pooled endpoint cannot be reached from CI, so the pooled code path is forced
(`is_pooled_endpoint` -> True) while connecting to the isolated `ap_agent_m8_test` database through the least-privilege
runtime role. Every connection made in these tests is checked to carry NO startup options, i.e. exactly what the pooler
requires; the limits must still be active, transaction-locally, and the existing tenant/RLS behaviour unchanged."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from ap_agent.db import connection as db
from ap_agent.db.connection import apply_transaction_timeouts, open_connection, set_tenant_context
from ap_agent.exceptions import TenantContextError

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

SETTINGS = "SELECT name, setting FROM pg_settings WHERE name IN ('statement_timeout', 'lock_timeout') ORDER BY name;"


@pytest.fixture()
def pooled(monkeypatch):
    """Force the pooled path and record the keyword arguments of every real connection."""

    connects: list[dict] = []
    real_connect = psycopg.connect

    def recording(dsn, **kwargs):
        connects.append(kwargs)
        return real_connect(dsn, **kwargs)

    monkeypatch.setattr(db, "is_pooled_endpoint", lambda dsn: True)
    monkeypatch.setattr(psycopg, "connect", recording)
    return connects


def _baseline(dsn) -> dict[str, str]:
    with psycopg.connect(dsn) as connection:
        return dict(connection.execute(SETTINGS).fetchall())


def test_a_pooled_connection_sends_no_options_but_enforces_both_limits(runtime_dsn, local_config, pooled):
    with open_connection(runtime_dsn, local_config) as connection:
        active = dict(connection.execute(SETTINGS).fetchall())

    assert active == {"lock_timeout": str(local_config.lock_timeout_milliseconds), "statement_timeout": str(local_config.statement_timeout_milliseconds)}
    assert pooled and all("options" not in kwargs for kwargs in pooled)


def test_the_limits_do_not_leak_to_the_next_transaction_connection_or_tenant(runtime_dsn, local_config, pooled, tenant_id):
    baseline = _baseline(runtime_dsn)

    with open_connection(runtime_dsn, local_config) as connection:
        set_tenant_context(connection.cursor(), str(tenant_id))

    # a brand-new connection (as another request / tenant would get from the pooler) sees the server defaults ...
    assert _baseline(runtime_dsn) == baseline and baseline["statement_timeout"] != str(local_config.statement_timeout_milliseconds)

    # ... and on ONE backend, after COMMIT the next transaction has the defaults again: nothing is session-level
    with psycopg.connect(runtime_dsn) as raw:
        apply_transaction_timeouts(raw, local_config)
        assert dict(raw.execute(SETTINGS).fetchall())["statement_timeout"] == str(local_config.statement_timeout_milliseconds)
        raw.commit()
        assert dict(raw.execute(SETTINGS).fetchall()) == baseline
        raw.execute("SELECT current_setting('ap_agent.tenant_id', true);")  # tenant context is equally gone
        assert raw.execute("SELECT nullif(current_setting('ap_agent.tenant_id', true), '');").fetchone()[0] is None


def test_a_rolled_back_transaction_also_discards_the_limits(runtime_dsn, local_config, pooled):
    baseline = _baseline(runtime_dsn)

    with psycopg.connect(runtime_dsn) as raw:
        apply_transaction_timeouts(raw, local_config)
        raw.rollback()
        assert dict(raw.execute(SETTINGS).fetchall()) == baseline


def test_the_statement_timeout_really_fires(runtime_dsn, local_config, pooled):
    from dataclasses import replace

    with pytest.raises(psycopg.errors.QueryCanceled):
        with open_connection(runtime_dsn, replace(local_config, statement_timeout_milliseconds=200)) as connection:
            connection.execute("SELECT pg_sleep(5);")


def test_tenant_context_and_row_level_security_are_unchanged(runtime_dsn, local_config, pooled, tenant_id):
    other = uuid.uuid4()

    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    PostgresMemoryRepository(runtime_dsn, local_config).register_tenant(tenant_id=other, tenant_key=f"m11e3-{other.hex[:8]}", display_name="Other")

    with open_connection(runtime_dsn, local_config) as connection:
        with connection.cursor() as cursor:
            set_tenant_context(cursor, str(tenant_id))
            cursor.execute("SELECT tenant_id FROM ap_agent.tenants;")
            assert [row[0] for row in cursor.fetchall()] == [tenant_id]  # only its own tenant row is visible

            with pytest.raises(psycopg.errors.InsufficientPrivilege):  # cross-tenant write is refused by the policy
                cursor.execute(
                    "INSERT INTO ap_agent.workflow_instances (workflow_id, tenant_id, batch_id, document_id, source_document_sha256, source_name, "
                    "current_phase, current_status) VALUES (%s, %s, %s, %s, %s, %s, 'REFERENCE_MATCHING', 'SUCCEEDED');",
                    (uuid.uuid4(), other, uuid.uuid4(), uuid.uuid4(), "c" * 64, "x.png"),
                )

    assert TenantContextError  # the verification helper is untouched
    assert all("options" not in kwargs for kwargs in pooled)


# -- every database-backed call path, through the pooled code path ----------------------------------------------------------------


def test_clerk_identity_resolution(runtime_dsn, local_config, pooled, tenant_id):
    from ap_agent.auth.identity import IdentityRepository

    repository = IdentityRepository(runtime_dsn, local_config)
    before = len(pooled)  # the tenant fixture already connected through the pooled path

    assert repository.resolve(provider="clerk", external_org_id="org_none", external_user_id="user_none") is None
    assert repository.tenant_display_name(tenant_id) == "M10 API Test Tenant"
    assert len(pooled) - before == 2 and all("options" not in kwargs for kwargs in pooled)


def test_dashboard_and_review_repository(runtime_dsn, local_config, pooled, tenant_id):
    from ap_agent.repositories.review_repository import ReviewRepository

    dashboard, rows = ReviewRepository(runtime_dsn, local_config).get_dashboard(tenant_id)  # `SET TRANSACTION READ ONLY` after the limits

    assert dashboard is not None and rows is not None
    assert all("options" not in kwargs for kwargs in pooled) and pooled


def test_operations_repository_api_and_worker_paths(runtime_dsn, local_config, pooled, tenant_id):
    from ap_agent.repositories.operations_repository import OperationsRepository

    operations = OperationsRepository(runtime_dsn, local_config)
    before = len(pooled)

    assert operations.get_job(tenant_id, uuid.uuid4()) is None  # API read path (tenant transaction, READ ONLY)
    assert operations.peek_job(uuid.uuid4()) is None  # Fargate worker re-read path (worker_transaction)
    assert operations.claim_job(uuid.uuid4(), tenant_id=tenant_id) is None  # worker claim path

    with operations.transaction(tenant_id) as cursor:
        cursor.execute(SETTINGS)
        assert dict(cursor.fetchall())["statement_timeout"] == str(local_config.statement_timeout_milliseconds)

    assert len(pooled) - before == 4 and all("options" not in kwargs for kwargs in pooled)


def test_readiness_probe_and_migration_runner_use_the_same_path(runtime_dsn, owner_dsn, local_config, pooled):
    from ap_agent.db.migration_runner import inspect_schema_migrations

    with open_connection(runtime_dsn, local_config) as connection:  # what /health/ready does
        assert connection.execute("SELECT 1;").fetchone() == (1,)

    assert inspect_schema_migrations(owner_dsn, local_config) is not None
    assert all("options" not in kwargs for kwargs in pooled)

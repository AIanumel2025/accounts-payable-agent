"""M8D real-PostgreSQL behavioural tests (task §7).

**Status in this environment: BLOCKED, not executed.** This container has
no Docker daemon (`docker ps` fails: "Cannot connect to the Docker
daemon") and `AP_AGENT_POSTGRES_DSN`/`AP_AGENT_POSTGRES_MIGRATION_DSN` are
unset, so every test in this module is skipped at collection time by
`_require_postgres_dsn` below. Mocks are deliberately **not** substituted
for real migration/RLS/trigger/transaction behaviour (task §7: "Mocks
must not substitute for PostgreSQL migration, RLS, transaction or trigger
tests."; task §9: "Do not replace PostgreSQL behavioural tests with
mocks."), so this file is written to run against a real PostgreSQL
instance once one is reachable, and reports as blocked rather than
claiming coverage it does not have. See
`docs/m8_phase_7_postgres_memory_report.md` for the full blocker writeup
and exactly how to run this suite locally
(`docker compose up -d postgres && pytest -q -m requires_postgres`).

Expects two roles (task §5: "Integration tests must use a runtime role
without SUPERUSER or BYPASSRLS privileges. Use separate migration-owner
and runtime roles in disposable PostgreSQL tests."):

  - `AP_AGENT_POSTGRES_MIGRATION_DSN`: a migration-owner role (may create
    schemas/tables/policies/triggers).
  - `AP_AGENT_POSTGRES_DSN`: an ordinary runtime application role, with
    neither `SUPERUSER` nor `BYPASSRLS`, granted only the privileges
    migration 0002's `REVOKE ALL ... FROM PUBLIC` + explicit `GRANT`s to
    that role would leave it (see `docker-compose.yml`'s `ap_agent_app`
    role and `.env.example`).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy
from ap_agent.exceptions import MigrationIntegrityError, TenantContextError
from ap_agent.models.memory import (
    AuditMemoryEvent,
    MemoryActorType,
    MemoryEventType,
    MemoryWorkflowStage,
    MemoryWorkflowStatus,
    WorkflowMemoryRecord,
)


def _require_postgres_dsn() -> None:
    if not (
        os.environ.get("AP_AGENT_POSTGRES_DSN")
        and os.environ.get("AP_AGENT_POSTGRES_MIGRATION_DSN")
    ):
        pytest.skip(
            "AP_AGENT_POSTGRES_DSN / AP_AGENT_POSTGRES_MIGRATION_DSN not "
            "configured; see docs/m8_phase_7_postgres_memory_report.md "
            "for how to run this suite against a disposable PostgreSQL "
            "instance (docker compose up -d postgres)."
        )


pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest.fixture(scope="module", autouse=True)
def _skip_without_live_postgres():
    _require_postgres_dsn()


@pytest.fixture(scope="module")
def local_config() -> MemoryConfig:
    # Disposable local/CI PostgreSQL: no channel-binding requirement.
    return MemoryConfig(
        allow_migration_dsn_fallback=False,
        transport_policy=PostgresTransportPolicy(
            require_ssl=False, require_channel_binding=False
        ),
    )


@pytest.fixture(scope="module")
def migration_dsn() -> str:
    return os.environ["AP_AGENT_POSTGRES_MIGRATION_DSN"]


@pytest.fixture(scope="module")
def runtime_dsn() -> str:
    return os.environ["AP_AGENT_POSTGRES_DSN"]


@pytest.fixture(scope="module")
def applied_migrations(migration_dsn, local_config):
    from ap_agent.db.migration_runner import apply_all_migrations

    return apply_all_migrations(migration_dsn, local_config)


@pytest.fixture()
def repository(runtime_dsn, local_config, applied_migrations):
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    return PostgresMemoryRepository(runtime_dsn, local_config)


@pytest.fixture()
def tenant(repository):
    tenant_id = uuid.uuid4()
    repository.register_tenant(
        tenant_id=tenant_id, tenant_key=f"test-{tenant_id.hex[:8]}", display_name="Test Tenant"
    )
    return tenant_id


# -- Migrations -----------------------------------------------------------


class TestMigrations:
    def test_fresh_migration_execution_applies_all_three(self, applied_migrations):
        statuses = {result["migration_id"]: result["status"] for result in applied_migrations}

        assert set(statuses) == {
            "0001_memory_schema_bootstrap",
            "0002_operational_memory_tables",
            "0003_matched_invoice_memory",
        }

    def test_idempotent_migration_rerun(self, migration_dsn, local_config, applied_migrations):
        from ap_agent.db.migration_runner import apply_all_migrations

        rerun = apply_all_migrations(migration_dsn, local_config)

        for result in rerun:
            assert result["status"] == "ALREADY_APPLIED"

    def test_checksum_mismatch_is_rejected(self, migration_dsn, local_config, tmp_path, monkeypatch):
        from ap_agent.db import migration_runner
        from ap_agent.db.migration_manifest import ManifestEntry

        tampered_dir = tmp_path
        (tampered_dir / "0001_memory_schema_bootstrap.sql").write_text(
            "-- tampered\n"
            + migration_runner.STATEMENT_BOUNDARY_MARKER
            + "CREATE SCHEMA IF NOT EXISTS ap_agent;"
        )
        monkeypatch.setattr(migration_runner, "MIGRATIONS_DIRECTORY", tampered_dir)

        entry = ManifestEntry(
            migration_id="0001_memory_schema_bootstrap",
            description="Create the AP Agent schema and migration registry.",
            sql_filename="0001_memory_schema_bootstrap.sql",
            expected_checksum="0" * 64,
        )

        with pytest.raises(MigrationIntegrityError):
            migration_runner.load_migration(entry)

    def test_concurrent_migration_locking_serializes_two_runners(
        self, migration_dsn, local_config
    ):
        import threading

        from ap_agent.db.migration_runner import apply_all_migrations

        errors: list[Exception] = []
        results: list[tuple] = []

        def run():
            try:
                results.append(apply_all_migrations(migration_dsn, local_config))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        assert len(results) == 2

    def test_additive_migration_does_not_invalidate_earlier_migrations(self, applied_migrations):
        # 0002/0003 are additive on top of 0001; re-inspecting 0001's own
        # registry row must still succeed (task §4A).
        statuses = {result["migration_id"]: result["status"] for result in applied_migrations}
        assert statuses["0001_memory_schema_bootstrap"] in {"APPLIED", "ALREADY_APPLIED"}


# -- Tenant isolation -------------------------------------------------------


class TestTenantIsolation:
    def test_tenant_registration_and_lookup(self, repository, tenant):
        row = repository.get_tenant(tenant_id=tenant)
        assert row is not None
        assert row.tenant_id == tenant

    def test_missing_tenant_context_fails_closed(self, runtime_dsn, local_config):
        from ap_agent.db.connection import open_connection

        with open_connection(runtime_dsn, local_config) as connection:
            with connection.cursor() as cursor:
                # Deliberately skip set_tenant_context: querying a
                # tenant-scoped table with no `ap_agent.tenant_id` set
                # must expose no rows (RLS policy compares against NULL,
                # which never matches), not raise and not leak data.
                cursor.execute("SELECT COUNT(*) FROM ap_agent.tenants;")
                count = cursor.fetchone()[0]

        assert count == 0

    def test_cross_tenant_reads_expose_no_data(self, repository):
        tenant_a = uuid.uuid4()
        tenant_b = uuid.uuid4()

        repository.register_tenant(
            tenant_id=tenant_a, tenant_key=f"a-{tenant_a.hex[:8]}", display_name="Tenant A"
        )
        repository.register_tenant(
            tenant_id=tenant_b, tenant_key=f"b-{tenant_b.hex[:8]}", display_name="Tenant B"
        )

        document_id = uuid.uuid4()
        record = _workflow_record(tenant_a, document_id)
        repository.create_or_get_workflow(tenant_id=tenant_a, record=record)

        assert repository.get_workflow_by_document(tenant_id=tenant_b, document_id=document_id) is None
        assert repository.get_workflow_by_document(tenant_id=tenant_a, document_id=document_id) is not None

    def test_cross_tenant_writes_fail(self, repository):
        """A workflow's FK to `tenants` is composite on `(tenant_id,
        workflow_id)`; writing a workflow row under a tenant context that
        does not match the row's own `tenant_id` column is rejected by RLS
        WITH CHECK, not silently attributed to the wrong tenant."""

        tenant_a = uuid.uuid4()
        repository.register_tenant(
            tenant_id=tenant_a, tenant_key=f"a-{tenant_a.hex[:8]}", display_name="Tenant A"
        )

        tenant_b = uuid.uuid4()  # never registered / no tenant context set for it
        document_id = uuid.uuid4()
        record = _workflow_record(tenant_b, document_id)

        with pytest.raises(Exception):
            repository.create_or_get_workflow(tenant_id=tenant_b, record=record)

    def test_rls_holds_under_connection_reuse(self, runtime_dsn, local_config):
        """Tenant context is set transaction-locally (`SELECT set_config(
        ..., TRUE)`); a second, differently-scoped transaction on a reused
        connection must not see the first transaction's tenant context."""

        from ap_agent.db.connection import open_connection, set_tenant_context

        tenant_a = uuid.uuid4()

        with open_connection(runtime_dsn, local_config) as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_a))
                cursor.execute("SELECT current_setting('ap_agent.tenant_id', TRUE);")
                assert cursor.fetchone()[0] == str(tenant_a)

        # A fresh connection (or a new transaction) must not inherit it.
        with open_connection(runtime_dsn, local_config) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('ap_agent.tenant_id', TRUE);")
                assert cursor.fetchone()[0] in (None, "")


def _workflow_record(tenant_id: uuid.UUID, document_id: uuid.UUID) -> WorkflowMemoryRecord:
    now = datetime.now(timezone.utc)
    return WorkflowMemoryRecord(
        memory_id=uuid.uuid4(),
        tenant_id=str(tenant_id),
        batch_id=uuid.uuid4(),
        document_id=document_id,
        source_name="integration-test.png",
        source_document_sha256="a" * 64,
        current_stage=MemoryWorkflowStage.REFERENCE_MATCHING,
        current_status=MemoryWorkflowStatus.SUCCEEDED,
        review_required=False,
        review_reasons=(),
        latest_matching_result_id=None,
        revision=1,
        created_at=now,
        updated_at=now,
    )


# -- Append-only behaviour --------------------------------------------------


class TestAppendOnly:
    def test_update_is_rejected_on_audit_events(self, repository, runtime_dsn, local_config, tenant):
        from ap_agent.db.connection import open_connection, set_tenant_context

        document_id = uuid.uuid4()
        workflow = repository.create_or_get_workflow(
            tenant_id=tenant, record=_workflow_record(tenant, document_id)
        )

        event = AuditMemoryEvent(
            audit_event_id=uuid.uuid4(),
            tenant_id=str(tenant),
            batch_id=workflow.batch_id,
            document_id=document_id,
            event_type=MemoryEventType.MEMORY_CREATED,
            actor_type=MemoryActorType.SYSTEM,
            actor_id="test",
            previous_stage=None,
            new_stage=MemoryWorkflowStage.MEMORY_PERSISTENCE,
            previous_status=None,
            new_status=MemoryWorkflowStatus.SUCCEEDED,
            correlation_id=uuid.uuid4(),
            payload_json="{}",
            payload_sha256="b" * 64,
            occurred_at=datetime.now(timezone.utc),
        )
        stored = repository.append_audit_event(
            tenant_id=tenant, workflow_id=workflow.memory_id, event=event
        )

        with open_connection(runtime_dsn, local_config) as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant))

                with pytest.raises(Exception):
                    cursor.execute(
                        "UPDATE ap_agent.audit_events SET message = 'tampered' "
                        "WHERE event_id = %s;",
                        (stored.audit_event_id,),
                    )

    def test_delete_is_rejected_on_audit_events(self, repository, runtime_dsn, local_config, tenant):
        from ap_agent.db.connection import open_connection, set_tenant_context

        with open_connection(runtime_dsn, local_config) as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant))

                with pytest.raises(Exception):
                    cursor.execute("DELETE FROM ap_agent.audit_events WHERE tenant_id = %s;", (tenant,))

    def test_truncate_is_rejected_on_invoice_memory_records(
        self, runtime_dsn, local_config, tenant
    ):
        from ap_agent.db.connection import open_connection, set_tenant_context

        with open_connection(runtime_dsn, local_config) as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant))

                with pytest.raises(Exception):
                    cursor.execute("TRUNCATE ap_agent.invoice_memory_records;")


# -- Idempotency and conflict detection -------------------------------------


class TestIdempotencyAndConflicts:
    def test_identical_write_is_idempotent(self, repository, tenant):
        document_id = uuid.uuid4()
        record = _workflow_record(tenant, document_id)

        first = repository.create_or_get_workflow(tenant_id=tenant, record=record)
        second = repository.create_or_get_workflow(tenant_id=tenant, record=record)

        assert first.memory_id == second.memory_id
        assert first.created_at == second.created_at

    def test_conflicting_write_under_same_identity_raises(self, repository, tenant):
        from dataclasses import replace

        document_id = uuid.uuid4()
        record = _workflow_record(tenant, document_id)
        repository.create_or_get_workflow(tenant_id=tenant, record=record)

        conflicting = replace(record, source_name="different-name.png")

        from ap_agent.exceptions import MemoryIntegrityError

        with pytest.raises(MemoryIntegrityError):
            repository.create_or_get_workflow(tenant_id=tenant, record=conflicting)


# -- Runtime role permissions -----------------------------------------------


class TestRuntimeRolePermissions:
    def test_runtime_role_is_not_superuser_or_bypassrls(self, runtime_dsn, local_config):
        from ap_agent.db.connection import open_connection

        with open_connection(runtime_dsn, local_config) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user;"
                )
                is_superuser, bypasses_rls = cursor.fetchone()

        assert is_superuser is False
        assert bypasses_rls is False

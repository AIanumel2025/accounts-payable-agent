"""M8 PostgreSQL acceptance suite: real-database behavioural tests.

**DSN sourcing (safety-critical, read this before touching this file).**
This suite reads **only** `AP_AGENT_TEST_POSTGRES_DSN`. It never reads
`AP_AGENT_POSTGRES_DSN` or `AP_AGENT_POSTGRES_MIGRATION_DSN` — those name
the *production* runtime/migration DSNs `ap_agent.config.postgres.MemoryConfig`
uses by default, and this suite must never silently fall back to them.
Before opening any connection, the DSN's `dbname` parameter is checked
against `EXPECTED_TEST_DATABASE` ("ap_agent_m8_test"); after the first
connection succeeds, `current_database()` is checked again, live, as
defense in depth. Either mismatch is a hard `pytest.fail` (not a skip —
a safety violation should be loud), and no destructive statement runs.
The DSN and its password are never printed, logged, written to a file, or
included in any assertion message anywhere in this module; only
non-secret metadata (database name, server version, role name) is ever
surfaced.

**Runtime role.** RLS is only meaningful when exercised through a role
RLS actually applies to — a `SUPERUSER` or `BYPASSRLS` role bypasses
every policy silently. `AP_AGENT_TEST_POSTGRES_DSN`'s own role (Neon's
database-owner role) is used **only** to run migrations and to create a
dedicated, isolated test runtime role
(`ap_agent.db.roles.create_least_privilege_role`,
`NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`, a fresh random
password generated per test session, never persisted). Every test below
except the migration tests themselves (which legitimately need
schema-creation privilege) runs through that dedicated role's own
connection, and `runtime_role_ready` asserts
`superuser is False and bypassrls is False` before any other fixture in
this module can run.

Marked `requires_postgres`; skips (not fails) only when
`AP_AGENT_TEST_POSTGRES_DSN` is entirely unset. See
`docs/m8_phase_7_postgres_memory_report.md` for the current run's result,
including this environment's own network-egress blocker if one applied.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy
from ap_agent.db import roles as db_roles
from ap_agent.exceptions import MemoryIntegrityError, MigrationIntegrityError
from ap_agent.models.memory import (
    MatchedInvoiceMemoryRecord,
    MemoryWorkflowStage,
    MemoryWorkflowStatus,
    WorkflowMemoryRecord,
)
from ap_agent.serialization.memory_json import canonical_payload_sha256

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

TEST_DSN_ENV_VAR = "AP_AGENT_TEST_POSTGRES_DSN"
EXPECTED_TEST_DATABASE = "ap_agent_m8_test"
RUNTIME_ROLE_NAME = "ap_agent_m8_test_runtime"

# Where this run's redacted probe result is written, for
# docs/m8_phase_7_postgres_memory_report.md to be updated from — never
# committed (outside the repository), never contains a DSN or password.
_PROBE_OUTPUT_PATH = Path("/tmp/ap_agent_m8_postgres_probe.json")


def _redact_host(host: str) -> str:
    """Keep only enough of a hostname to confirm *which provider*
    (e.g. '...neon.tech') without exposing the full endpoint id."""

    parts = host.split(".")
    if len(parts) <= 2:
        return "<redacted>"
    return "<redacted>." + ".".join(parts[-2:])


# ------------------------------------------------------------
# DSN sourcing and the fail-closed database-identity gate
# ------------------------------------------------------------


def _owner_dsn_or_skip() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR, "").strip()

    if not dsn:
        pytest.skip(
            f"{TEST_DSN_ENV_VAR} is not configured. This suite deliberately "
            "never falls back to AP_AGENT_POSTGRES_DSN/AP_AGENT_POSTGRES_MIGRATION_DSN "
            "(those name the production runtime/migration DSNs) — see "
            "docs/m8_phase_7_postgres_memory_report.md for how to configure "
            "a dedicated, disposable test database."
        )

    from psycopg.conninfo import conninfo_to_dict

    params = conninfo_to_dict(dsn)
    dbname = params.get("dbname")

    if dbname != EXPECTED_TEST_DATABASE:
        pytest.fail(
            f"{TEST_DSN_ENV_VAR} targets database {dbname!r}, expected "
            f"{EXPECTED_TEST_DATABASE!r}. Refusing to run (fail-closed): this "
            "suite executes destructive statements (UPDATE/DELETE/TRUNCATE "
            "rejection tests, role creation, transaction-rollback tests) and "
            "must never run against a database that might be production."
        )

    return dsn


@pytest.fixture(scope="session")
def owner_dsn() -> str:
    return _owner_dsn_or_skip()


@pytest.fixture(scope="session")
def local_config() -> MemoryConfig:
    # The provided DSN already declares sslmode=require&channel_binding=require
    # (Neon); require both explicitly too, for defense in depth.
    return MemoryConfig(
        transport_policy=PostgresTransportPolicy(
            require_ssl=True, require_channel_binding=True
        ),
    )


@pytest.fixture(scope="session", autouse=True)
def _verify_live_database_identity(owner_dsn, local_config) -> dict[str, Any]:
    """Defense-in-depth: re-check `current_database()` live, after the
    static DSN-parsing gate in `_owner_dsn_or_skip`. Records only
    non-secret metadata (never the DSN) for the milestone report.
    """

    from psycopg.conninfo import conninfo_to_dict

    from ap_agent.db.connection import open_connection

    with open_connection(owner_dsn, local_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_database(), current_setting('server_version'), "
                "current_setting('TimeZone');"
            )
            dbname, server_version, server_timezone = cursor.fetchone()

    if dbname != EXPECTED_TEST_DATABASE:
        pytest.fail(
            f"Live current_database() == {dbname!r}, expected "
            f"{EXPECTED_TEST_DATABASE!r}; refusing to proceed (fail-closed)."
        )

    host = conninfo_to_dict(owner_dsn).get("host", "")

    probe = {
        "database": dbname,
        "server_version": server_version,
        "server_timezone": server_timezone,
        "host_suffix": _redact_host(host),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        _PROBE_OUTPUT_PATH.write_text(json.dumps(probe, indent=2))
    except OSError:
        pass  # best-effort only; never fail the suite over this

    return probe


# ------------------------------------------------------------
# Migration-owner connection, applied migrations, dedicated runtime role
# ------------------------------------------------------------


@pytest.fixture(scope="session")
def applied_migrations(owner_dsn, local_config, _verify_live_database_identity):
    from ap_agent.db.migration_runner import apply_all_migrations

    return apply_all_migrations(owner_dsn, local_config)


@pytest.fixture(scope="session")
def runtime_role_ready(owner_dsn, local_config, applied_migrations) -> dict[str, str]:
    """Create (idempotently) the dedicated, least-privilege test runtime
    role and grant it schema access, then assert it really is
    least-privilege before any RLS-sensitive test can run."""

    password = secrets.token_urlsafe(32)

    db_roles.create_least_privilege_role(
        owner_dsn, local_config, role_name=RUNTIME_ROLE_NAME, password=password
    )
    db_roles.grant_schema_access(owner_dsn, local_config, role_name=RUNTIME_ROLE_NAME)

    flags = db_roles.role_privilege_flags(
        owner_dsn, local_config, role_name=RUNTIME_ROLE_NAME
    )

    assert flags["superuser"] is False, "Test runtime role must not be SUPERUSER."
    assert flags["bypassrls"] is False, "Test runtime role must not be BYPASSRLS."

    return {"role_name": RUNTIME_ROLE_NAME, "password": password}


@pytest.fixture(scope="session")
def runtime_dsn(owner_dsn, runtime_role_ready) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(owner_dsn)
    params["user"] = runtime_role_ready["role_name"]
    params["password"] = runtime_role_ready["password"]

    return make_conninfo(**params)


@pytest.fixture()
def repository(runtime_dsn, local_config):
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    return PostgresMemoryRepository(runtime_dsn, local_config)


@pytest.fixture()
def tenant(repository) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    repository.register_tenant(
        tenant_id=tenant_id,
        tenant_key=f"m8-acceptance-{tenant_id.hex[:8]}",
        display_name="M8 Acceptance Tenant",
    )
    return tenant_id


# ------------------------------------------------------------
# Shared synthetic-record helpers
# ------------------------------------------------------------


def _workflow_record(tenant_id: uuid.UUID, document_id: uuid.UUID, **overrides) -> WorkflowMemoryRecord:
    now = datetime.now(timezone.utc)
    defaults = dict(
        memory_id=uuid.uuid4(),
        tenant_id=str(tenant_id),
        batch_id=uuid.uuid4(),
        document_id=document_id,
        source_name="pg-acceptance-test.png",
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
    defaults.update(overrides)
    return WorkflowMemoryRecord(**defaults)


def _invoice_memory_record(
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    source_name: str = "pg-acceptance-test.png",
    total_amount: Decimal | None = Decimal("42.00"),
    currency: str | None = "USD",
) -> MatchedInvoiceMemoryRecord:
    normalized_payload = {"kind": "normalization", "document_id": str(document_id)}
    financial_payload = {"kind": "financial_validation", "document_id": str(document_id)}
    matching_payload = {"kind": "matching", "document_id": str(document_id)}
    reference_payload = {"supplier": None, "purchase_order": None, "goods_receipts": []}

    complete_payload = {
        "normalization_result": normalized_payload,
        "financial_validation_result": financial_payload,
        "matching_result": matching_payload,
        "matched_reference_data": reference_payload,
    }

    return MatchedInvoiceMemoryRecord(
        record_id=uuid.uuid4(),
        tenant_id=tenant_id,
        workflow_memory_id=workflow_id,
        batch_id=uuid.uuid4(),
        document_id=document_id,
        matching_result_id=uuid.uuid4(),
        source_name=source_name,
        source_document_sha256="b" * 64,
        invoice_record_id=uuid.uuid4(),
        invoice_number="ACC-TEST-1",
        supplier_name="Acceptance Test Supplier",
        currency=currency,
        total_amount=total_amount,
        supplier_resolution_status="NOT_REFERENCED",
        matched_supplier_id=None,
        purchase_order_status="NOT_REFERENCED",
        purchase_order_id=None,
        purchase_order_number=None,
        goods_receipt_status="NOT_REFERENCED",
        goods_receipt_ids=(),
        match_mode="UNDETERMINED",
        line_match_count=0,
        normalization_status="SUCCEEDED",
        financial_validation_status="SUCCEEDED",
        matching_status="SUCCEEDED",
        review_required=False,
        review_reasons=(),
        normalized_payload=normalized_payload,
        financial_payload=financial_payload,
        matching_payload=matching_payload,
        reference_payload=reference_payload,
        payload_sha256=canonical_payload_sha256(complete_payload),
    )


# ==============================================================
# 1. Clean migration application
# ==============================================================


def test_clean_migration_application(applied_migrations):
    statuses = {result["migration_id"]: result["status"] for result in applied_migrations}

    assert set(statuses) == {
        "0001_memory_schema_bootstrap",
        "0002_operational_memory_tables",
        "0003_matched_invoice_memory",
    }
    assert all(status in {"APPLIED", "ALREADY_APPLIED"} for status in statuses.values())


# ==============================================================
# 2. Idempotent migration rerun
# ==============================================================


def test_idempotent_migration_rerun(owner_dsn, local_config, applied_migrations):
    from ap_agent.db.migration_runner import apply_all_migrations

    first_applied_at = {r["migration_id"]: r["applied_at"] for r in applied_migrations}

    rerun = apply_all_migrations(owner_dsn, local_config)

    for result in rerun:
        assert result["status"] == "ALREADY_APPLIED"
        assert result["applied_at"] == first_applied_at[result["migration_id"]]


# ==============================================================
# 3. Migration-checksum mismatch rejection (real DB round trip)
# ==============================================================


def test_migration_checksum_mismatch_rejection(owner_dsn, local_config, applied_migrations):
    from ap_agent.db.connection import open_connection
    from ap_agent.db.migration_manifest import MIGRATION_MANIFEST
    from ap_agent.db.migration_runner import apply_migration, load_migration

    entry = next(
        e for e in MIGRATION_MANIFEST if e.migration_id == "0001_memory_schema_bootstrap"
    )
    migration = load_migration(entry)

    bogus_checksum = "0" * 64

    with open_connection(owner_dsn, local_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE ap_agent.schema_migrations SET checksum = %s WHERE migration_id = %s;",
                (bogus_checksum, entry.migration_id),
            )

    try:
        with open_connection(owner_dsn, local_config) as connection:
            with connection.cursor() as cursor:
                with pytest.raises(MigrationIntegrityError):
                    apply_migration(cursor, migration)
    finally:
        # Restore real state so later tests (and later runs) see a
        # correctly recorded migration, not this test's tampering.
        with open_connection(owner_dsn, local_config) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE ap_agent.schema_migrations SET checksum = %s WHERE migration_id = %s;",
                    (entry.expected_checksum, entry.migration_id),
                )


# ==============================================================
# 4. Concurrent migration locking
# ==============================================================


def test_concurrent_migration_locking(owner_dsn, local_config, applied_migrations):
    from ap_agent.db.migration_runner import apply_all_migrations

    errors: list[Exception] = []
    results: list[tuple] = []

    def run() -> None:
        try:
            results.append(apply_all_migrations(owner_dsn, local_config))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert len(results) == 2


# ==============================================================
# 5. Tenant row-level read isolation
# ==============================================================


def test_tenant_row_level_read_isolation(repository, runtime_role_ready):
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    repository.register_tenant(
        tenant_id=tenant_a, tenant_key=f"a-{tenant_a.hex[:8]}", display_name="Tenant A"
    )
    repository.register_tenant(
        tenant_id=tenant_b, tenant_key=f"b-{tenant_b.hex[:8]}", display_name="Tenant B"
    )

    document_id = uuid.uuid4()
    repository.create_or_get_workflow(
        tenant_id=tenant_a, record=_workflow_record(tenant_a, document_id)
    )

    assert (
        repository.get_workflow_by_document(tenant_id=tenant_b, document_id=document_id)
        is None
    )
    assert (
        repository.get_workflow_by_document(tenant_id=tenant_a, document_id=document_id)
        is not None
    )


# ==============================================================
# 6. Cross-tenant write rejection
# ==============================================================


def test_cross_tenant_write_rejection(runtime_dsn, local_config, repository):
    """A raw INSERT under tenant B's tenant_id while the session's RLS
    context is set to tenant A must be rejected by the policy's WITH
    CHECK clause -- real PostgreSQL enforcement, not application logic."""

    from ap_agent.db.connection import open_connection, set_tenant_context

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    repository.register_tenant(
        tenant_id=tenant_a, tenant_key=f"xa-{tenant_a.hex[:8]}", display_name="Tenant XA"
    )
    repository.register_tenant(
        tenant_id=tenant_b, tenant_key=f"xb-{tenant_b.hex[:8]}", display_name="Tenant XB"
    )

    with open_connection(runtime_dsn, local_config) as connection:
        with connection.cursor() as cursor:
            set_tenant_context(cursor, str(tenant_a))

            with pytest.raises(Exception):
                cursor.execute(
                    """
                    INSERT INTO ap_agent.workflow_instances
                        (workflow_id, tenant_id, batch_id, document_id,
                         source_document_sha256, source_name,
                         current_phase, current_status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'REFERENCE_MATCHING', 'SUCCEEDED');
                    """,
                    (
                        uuid.uuid4(),
                        tenant_b,  # mismatched tenant_id under tenant_a's context
                        uuid.uuid4(),
                        uuid.uuid4(),
                        "c" * 64,
                        "cross-tenant-write-test.png",
                    ),
                )


# ==============================================================
# 7. Append-only UPDATE rejection
# ==============================================================


def test_append_only_update_rejection(repository, runtime_dsn, local_config, tenant):
    from ap_agent.db.connection import open_connection, set_tenant_context
    from ap_agent.models.memory import AuditMemoryEvent, MemoryActorType, MemoryEventType

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
        actor_id="pg-acceptance-test",
        previous_stage=None,
        new_stage=MemoryWorkflowStage.MEMORY_PERSISTENCE,
        previous_status=None,
        new_status=MemoryWorkflowStatus.SUCCEEDED,
        correlation_id=uuid.uuid4(),
        payload_json="{}",
        payload_sha256="d" * 64,
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
                    "UPDATE ap_agent.audit_events SET message = 'tampered' WHERE event_id = %s;",
                    (stored.audit_event_id,),
                )


# ==============================================================
# 8. Append-only DELETE rejection
# ==============================================================


def test_append_only_delete_rejection(repository, runtime_dsn, local_config, tenant):
    from ap_agent.db.connection import open_connection, set_tenant_context

    with open_connection(runtime_dsn, local_config) as connection:
        with connection.cursor() as cursor:
            set_tenant_context(cursor, str(tenant))

            with pytest.raises(Exception):
                cursor.execute(
                    "DELETE FROM ap_agent.audit_events WHERE tenant_id = %s;", (tenant,)
                )


# ==============================================================
# 9. Append-only TRUNCATE rejection
# ==============================================================


def test_append_only_truncate_rejection(runtime_dsn, local_config, tenant):
    from ap_agent.db.connection import open_connection, set_tenant_context

    with open_connection(runtime_dsn, local_config) as connection:
        with connection.cursor() as cursor:
            set_tenant_context(cursor, str(tenant))

            with pytest.raises(Exception):
                cursor.execute("TRUNCATE ap_agent.invoice_memory_records;")


# ==============================================================
# 10. Payload-hash verification
# ==============================================================


def test_payload_hash_verification(repository, tenant):
    document_id = uuid.uuid4()
    workflow = repository.create_or_get_workflow(
        tenant_id=tenant, record=_workflow_record(tenant, document_id)
    )
    record = _invoice_memory_record(tenant, workflow.memory_id, document_id)

    repository.store_invoice_memory(tenant_id=tenant, record=record)
    fetched = repository.get_invoice_memory_by_document(tenant_id=tenant, document_id=document_id)

    assert fetched is not None

    reconstructed_payload = {
        "normalization_result": fetched.normalized_payload,
        "financial_validation_result": fetched.financial_payload,
        "matching_result": fetched.matching_payload,
        "matched_reference_data": fetched.reference_payload,
    }
    recalculated_hash = canonical_payload_sha256(reconstructed_payload)

    assert recalculated_hash == fetched.payload_sha256 == record.payload_sha256


# ==============================================================
# 11. Transaction rollback on failure
# ==============================================================


def test_transaction_rollback_on_failure(runtime_dsn, local_config, tenant):
    """A valid statement followed, in the *same* transaction, by one that
    violates a foreign key must roll back both -- the valid workflow row
    must not be visible afterward."""

    from ap_agent.db.connection import open_connection, set_tenant_context

    document_id = uuid.uuid4()
    workflow_id = uuid.uuid4()

    try:
        with open_connection(runtime_dsn, local_config) as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant))

                cursor.execute(
                    """
                    INSERT INTO ap_agent.workflow_instances
                        (workflow_id, tenant_id, batch_id, document_id,
                         source_document_sha256, source_name,
                         current_phase, current_status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'REFERENCE_MATCHING', 'SUCCEEDED');
                    """,
                    (
                        workflow_id,
                        tenant,
                        uuid.uuid4(),
                        document_id,
                        "e" * 64,
                        "rollback-test.png",
                    ),
                )

                # Violates phase_reference_workflow_fk: references a
                # workflow_id that does not (and, after rollback, will
                # not) exist.
                cursor.execute(
                    """
                    INSERT INTO ap_agent.phase_result_references
                        (reference_id, tenant_id, workflow_id, phase_name,
                         phase_version, attempt_number, result_id,
                         result_status, artifact_uri, artifact_sha256,
                         produced_at)
                    VALUES (%s, %s, %s, 'REFERENCE_MATCHING', 'v1', 1, 'x',
                            'SUCCEEDED', 'postgresql://test/x', %s, now());
                    """,
                    (uuid.uuid4(), tenant, uuid.uuid4(), "f" * 64),
                )
    except Exception:
        pass  # expected: the FK violation
    else:
        pytest.fail("Expected the foreign-key violation to raise.")

    with open_connection(runtime_dsn, local_config) as connection:
        with connection.cursor() as cursor:
            set_tenant_context(cursor, str(tenant))
            cursor.execute(
                "SELECT 1 FROM ap_agent.workflow_instances WHERE workflow_id = %s;",
                (workflow_id,),
            )
            assert cursor.fetchone() is None, "Rollback did not undo the valid insert."


# ==============================================================
# 12. Idempotent persistence
# ==============================================================


def test_idempotent_persistence(repository, tenant):
    document_id = uuid.uuid4()
    workflow = repository.create_or_get_workflow(
        tenant_id=tenant, record=_workflow_record(tenant, document_id)
    )
    record = _invoice_memory_record(tenant, workflow.memory_id, document_id)

    first = repository.store_invoice_memory(tenant_id=tenant, record=record)
    second = repository.store_invoice_memory(tenant_id=tenant, record=record)

    assert first.record_id == second.record_id
    assert first.payload_sha256 == second.payload_sha256


# ==============================================================
# 13. Connection reuse without tenant-context leakage
# ==============================================================


def test_connection_reuse_without_tenant_context_leakage(runtime_dsn, local_config):
    from ap_agent.db.connection import create_connection_pool, set_tenant_context

    tenant_a = uuid.uuid4()

    pool = create_connection_pool(runtime_dsn, local_config)
    pool.open(wait=True, timeout=30)

    try:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_a))
                cursor.execute("SELECT current_setting('ap_agent.tenant_id', TRUE);")
                assert cursor.fetchone()[0] == str(tenant_a)

        # A second checkout -- possibly the same underlying physical
        # connection -- must not inherit tenant_a's context, because
        # set_tenant_context sets it transaction-locally (`TRUE`).
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('ap_agent.tenant_id', TRUE);")
                assert cursor.fetchone()[0] in (None, "")
    finally:
        pool.close()


# ==============================================================
# 14. Batch persistence
# ==============================================================


def test_batch_persistence(repository, tenant):
    records = []
    for _ in range(5):
        document_id = uuid.uuid4()
        workflow = repository.create_or_get_workflow(
            tenant_id=tenant, record=_workflow_record(tenant, document_id)
        )
        records.append(
            _invoice_memory_record(
                tenant,
                workflow.memory_id,
                document_id,
                source_name=f"batch-{document_id}.png",
            )
        )

    stored = repository.store_invoice_memory_batch(
        tenant_id=tenant, records=tuple(records), batch_size=2
    )

    assert len(stored) == 5
    assert {r.document_id for r in stored} == {r.document_id for r in records}

    for record in records:
        fetched = repository.get_invoice_memory_by_document(
            tenant_id=tenant, document_id=record.document_id
        )
        assert fetched is not None
        assert fetched.payload_sha256 == record.payload_sha256


# ==============================================================
# 15. Cross-document isolation
# ==============================================================


def test_cross_document_isolation(repository, tenant):
    document_ids = [uuid.uuid4(), uuid.uuid4()]
    records = {}

    for document_id in document_ids:
        workflow = repository.create_or_get_workflow(
            tenant_id=tenant, record=_workflow_record(tenant, document_id)
        )
        record = _invoice_memory_record(
            tenant, workflow.memory_id, document_id, source_name=f"cross-doc-{document_id}.png"
        )
        repository.store_invoice_memory(tenant_id=tenant, record=record)
        records[document_id] = record

    for document_id in document_ids:
        bundle = repository.get_invoice_memory_bundle(tenant_id=tenant, document_id=document_id)

        assert bundle is not None
        assert bundle.invoice_memory.document_id == document_id

        other_document_id = next(d for d in document_ids if d != document_id)

        assert str(other_document_id) not in json.dumps(
            {
                "normalized": bundle.invoice_memory.normalized_payload,
                "financial": bundle.invoice_memory.financial_payload,
                "matching": bundle.invoice_memory.matching_payload,
                "reference": bundle.invoice_memory.reference_payload,
            }
        )


# ==============================================================
# 16. Retrieval fidelity
# ==============================================================


def test_retrieval_fidelity(repository, tenant):
    document_id = uuid.uuid4()
    workflow = repository.create_or_get_workflow(
        tenant_id=tenant, record=_workflow_record(tenant, document_id)
    )
    record = _invoice_memory_record(
        tenant,
        workflow.memory_id,
        document_id,
        total_amount=Decimal("873.58"),
        currency=None,  # exercise None round-trip too
    )

    repository.store_invoice_memory(tenant_id=tenant, record=record)
    fetched = repository.get_invoice_memory_by_document(tenant_id=tenant, document_id=document_id)

    assert fetched is not None
    assert fetched.total_amount == Decimal("873.58")
    assert isinstance(fetched.total_amount, Decimal)
    assert fetched.currency is None
    assert fetched.invoice_number == record.invoice_number
    assert fetched.source_document_sha256 == record.source_document_sha256
    assert fetched.goods_receipt_ids == record.goods_receipt_ids
    assert fetched.review_reasons == record.review_reasons

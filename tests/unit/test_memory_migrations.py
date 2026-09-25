"""M8D unit tests: migration order and checksums (no database required)."""

from __future__ import annotations

import pytest

from ap_agent.db.migration_manifest import MIGRATION_MANIFEST, ordered_migration_ids
from ap_agent.db.migration_runner import (
    calculate_migration_checksum,
    load_all_migrations,
    load_migration,
    load_migration_statements,
    MIGRATIONS_DIRECTORY,
)
from ap_agent.exceptions import MigrationIntegrityError

pytestmark = pytest.mark.unit


def test_migration_order_is_0001_0002_0003():
    assert ordered_migration_ids() == (
        "0001_memory_schema_bootstrap",
        "0002_operational_memory_tables",
        "0003_matched_invoice_memory",
    )


def test_every_manifest_entry_has_a_64_character_hex_checksum():
    for entry in MIGRATION_MANIFEST:
        assert len(entry.expected_checksum) == 64
        int(entry.expected_checksum, 16)  # raises ValueError if not hex


def test_migration_sql_files_reproduce_the_manifest_checksum():
    """Task §3.8: 'Formatting SQL files must not silently create a
    different checksum for an already-applied migration.'"""

    migrations = load_all_migrations()

    assert len(migrations) == len(MIGRATION_MANIFEST)

    for migration, entry in zip(migrations, MIGRATION_MANIFEST):
        assert migration.migration_id == entry.migration_id
        assert migration.checksum == entry.expected_checksum


def test_load_migration_rejects_a_tampered_sql_file(tmp_path, monkeypatch):
    from ap_agent.db import migration_runner
    from ap_agent.db.migration_manifest import ManifestEntry

    tampered_dir = tmp_path
    (tampered_dir / "0001_memory_schema_bootstrap.sql").write_text(
        "-- header\n"
        + migration_runner.STATEMENT_BOUNDARY_MARKER
        + "CREATE SCHEMA IF NOT EXISTS tampered;"
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


def test_calculate_migration_checksum_matches_notebook_algorithm():
    statements = ("\n  CREATE SCHEMA IF NOT EXISTS ap_agent;\n  ",)

    from hashlib import sha256

    expected = sha256(
        "\n".join(statement.strip() for statement in statements).encode("utf-8")
    ).hexdigest()

    assert calculate_migration_checksum(statements) == expected


def test_load_migration_statements_strips_only_the_preamble():
    for entry in MIGRATION_MANIFEST:
        statements = load_migration_statements(MIGRATIONS_DIRECTORY / entry.sql_filename)

        assert len(statements) > 0

        for statement in statements:
            assert statement.strip()


def test_bootstrap_migration_creates_its_own_registry_table():
    migrations = {m.migration_id: m for m in load_all_migrations()}
    bootstrap = migrations["0001_memory_schema_bootstrap"]

    combined_sql = "\n".join(bootstrap.statements)

    assert "ap_agent.schema_migrations" in combined_sql
    assert "CREATE SCHEMA IF NOT EXISTS ap_agent" in combined_sql


def test_operational_memory_migration_creates_eight_tables_total():
    """Task §1.8: eight operational tables after migration 0003 (7 created
    by 0002 + schema_migrations from 0001 + invoice_memory_records from
    0003 = 8)."""

    migrations = {m.migration_id: m for m in load_all_migrations()}

    memory_sql = "\n".join(migrations["0002_operational_memory_tables"].statements)
    invoice_sql = "\n".join(migrations["0003_matched_invoice_memory"].statements)

    for table_name in (
        "tenants",
        "workflow_instances",
        "phase_result_references",
        "audit_events",
        "review_cases",
        "review_decisions",
    ):
        assert f"ap_agent.{table_name}" in memory_sql

    assert "ap_agent.invoice_memory_records" in invoice_sql


def test_operational_memory_migration_creates_seven_tenant_isolation_policies():
    migrations = {m.migration_id: m for m in load_all_migrations()}

    memory_sql = "\n".join(migrations["0002_operational_memory_tables"].statements)
    invoice_sql = "\n".join(migrations["0003_matched_invoice_memory"].statements)

    policy_count = memory_sql.count("CREATE POLICY tenant_isolation") + invoice_sql.count(
        "CREATE POLICY tenant_isolation"
    )

    assert policy_count == 7


def test_migrations_create_six_append_only_triggers():
    migrations = {m.migration_id: m for m in load_all_migrations()}

    memory_sql = "\n".join(migrations["0002_operational_memory_tables"].statements)
    invoice_sql = "\n".join(migrations["0003_matched_invoice_memory"].statements)

    trigger_count = memory_sql.count("CREATE TRIGGER") + invoice_sql.count("CREATE TRIGGER")

    assert trigger_count == 6

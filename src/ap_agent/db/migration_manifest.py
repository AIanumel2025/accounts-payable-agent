"""Immutable migration manifest for Phase 7 operational memory.

Source: notebook cell 85 ("PHASE 7 -- CELL 3")'s `BOOTSTRAP_MIGRATION_ID`/
`BOOTSTRAP_MIGRATION_DESCRIPTION`, cell 86 ("PHASE 7 -- CELL 4")'s
`MEMORY_MIGRATION_ID`/`MEMORY_MIGRATION_DESCRIPTION`, and cell 88
("PHASE 7 -- REPLACEMENT CELL 5")'s `INVOICE_MEMORY_MIGRATION_ID`/
`INVOICE_MEMORY_MIGRATION_DESCRIPTION`.

Task brief SS3.8 requires that "Formatting SQL files must not silently
create a different checksum for an already-applied migration" and that a
manifest hold "the immutable expected checksum for each migration". This
module is that manifest: each entry's `expected_checksum` is the exact
SHA-256 the notebook computed for the corresponding migration (verified by
extracting the notebook's `BOOTSTRAP_STATEMENTS` / `MEMORY_MIGRATION_STATEMENTS`
/ `INVOICE_MEMORY_MIGRATION_STATEMENTS` tuples and reproducing
`calculate_migration_checksum`/`canonical_migration_sql`/
`canonicalize_migration` -- all three are the same algorithm:
`sha256("\\n".join(statement.strip() for statement in statements))`).

`ap_agent.db.migration_runner` recomputes this same checksum from the
`.sql` files under `ap_agent/db/migrations/` at runtime and compares it
against `expected_checksum` here *before* comparing it against whatever a
target database already has recorded -- so an accidental reformat of a
migration file is caught locally, without ever touching a database.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ManifestEntry",
    "MIGRATION_MANIFEST",
    "ordered_migration_ids",
]


@dataclass(frozen=True)
class ManifestEntry:
    migration_id: str
    description: str
    sql_filename: str
    expected_checksum: str


MIGRATION_MANIFEST: tuple[ManifestEntry, ...] = (
    ManifestEntry(
        migration_id="0001_memory_schema_bootstrap",
        description="Create the AP Agent schema and migration registry.",
        sql_filename="0001_memory_schema_bootstrap.sql",
        expected_checksum="4e26c8aabf36a08d462a58a59bb1e131f075c848fe1e85dc336b59622ced55f1",
    ),
    ManifestEntry(
        migration_id="0002_operational_memory_tables",
        description=(
            "Create tenant-isolated workflow, phase-reference, "
            "audit and human-review memory tables."
        ),
        sql_filename="0002_operational_memory_tables.sql",
        expected_checksum="3e31a0fcc5d5a3b5c1716981a6f1ee46d32e01cb53b229fdb7001d7b46135838",
    ),
    ManifestEntry(
        migration_id="0003_matched_invoice_memory",
        description=(
            "Persist normalized, financially validated and "
            "reference-matched invoice records."
        ),
        sql_filename="0003_matched_invoice_memory.sql",
        expected_checksum="89d65212323f1fa2cef8993e1b3540b296c0b7ac50ccce497704694965e1f659",
    ),
    ManifestEntry(
        migration_id="0004_durable_operations",
        description=(
            "Add the single-worker workflow job queue, append-only job events, "
            "derived invoice-memory versions and effective-memory views (M11D Core)."
        ),
        sql_filename="0004_durable_operations.sql",
        expected_checksum="a036689007db187f05c3fcc6b02a5a56dee752f14f558ce90a6e054aa463ff6e",
    ),
)


def ordered_migration_ids() -> tuple[str, ...]:
    return tuple(entry.migration_id for entry in MIGRATION_MANIFEST)

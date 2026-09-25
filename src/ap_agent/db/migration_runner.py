"""PostgreSQL migration runner for Phase 7 operational memory.

Source: notebook cell 85 ("PHASE 7 — CELL 3")'s
`calculate_migration_checksum`, `create_advisory_lock_key`,
`apply_bootstrap_migration`, `inspect_memory_schema`; cell 86
("PHASE 7 — CELL 4")'s `canonical_migration_sql`,
`apply_memory_schema_migration`, `inspect_operational_memory_schema`;
and cell 88 ("PHASE 7 — REPLACEMENT CELL 5")'s `canonicalize_migration`,
`apply_invoice_memory_migration`. All three notebook "apply" functions are
the *same* transactional-lock / checksum-check / insert-once algorithm
duplicated three times (once per migration); this module extracts it once
as `apply_migration`/`apply_all_migrations` and applies it to every entry
in `ap_agent.db.migration_manifest.MIGRATION_MANIFEST` in order, instead
of one bespoke function per migration id.

Behaviour preserved verbatim from the notebook:
  - `pg_advisory_xact_lock` is taken before reading or writing
    `ap_agent.schema_migrations`, using the same lock key derivation
    (`sha256(lock_name)[:8]` as a signed bigint) as
    `create_advisory_lock_key("ap_agent.schema_migrations")`.
  - A migration already recorded is re-verified against the checksum and
    description before being reported `ALREADY_APPLIED`; a mismatch on
    either raises fail-closed (`MigrationIntegrityError`, replacing the
    notebook's bare `RuntimeError` — see `ap_agent.exceptions`).
  - Applying is exactly-once per migration id; rerunning is idempotent
    (`applied_at` does not change on a second run).

New in M8 (task §4A): the checksum is *first* checked against
`ap_agent.db.migration_manifest.MIGRATION_MANIFEST`'s immutable
`expected_checksum` before ever touching a database, so a reformatted
`.sql` file is caught locally. The SQL is loaded from
`ap_agent/db/migrations/*.sql` (marker-delimited per-statement blocks,
see each file's header) rather than being a Python string tuple, per the
task's required file layout (§4A).
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ap_agent.config.postgres import MemoryConfig
from ap_agent.db.connection import open_connection
from ap_agent.db.migration_manifest import MIGRATION_MANIFEST, ManifestEntry
from ap_agent.exceptions import MigrationIntegrityError

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

__all__ = [
    "MIGRATIONS_DIRECTORY",
    "STATEMENT_BOUNDARY_MARKER",
    "LoadedMigration",
    "load_migration_statements",
    "canonical_migration_sql",
    "calculate_migration_checksum",
    "create_advisory_lock_key",
    "load_migration",
    "load_all_migrations",
    "apply_bootstrap_registry",
    "apply_migration",
    "apply_all_migrations",
    "inspect_schema_migrations",
]


MIGRATIONS_DIRECTORY = Path(__file__).parent / "migrations"

# Matches each `.sql` file's own header comment; see e.g.
# `0001_memory_schema_bootstrap.sql`.
STATEMENT_BOUNDARY_MARKER = "\n-- ===AP_AGENT_MIGRATION_STATEMENT_BOUNDARY===\n"

# The bootstrap migration creates `ap_agent.schema_migrations` itself, so
# applying it is special-cased (no registry row to check against yet).
BOOTSTRAP_MIGRATION_ID = "0001_memory_schema_bootstrap"


class LoadedMigration:
    __slots__ = ("migration_id", "description", "statements", "checksum")

    def __init__(
        self,
        migration_id: str,
        description: str,
        statements: tuple[str, ...],
        checksum: str,
    ) -> None:
        self.migration_id = migration_id
        self.description = description
        self.statements = statements
        self.checksum = checksum


def load_migration_statements(sql_path: Path) -> tuple[str, ...]:
    """Split a migration `.sql` file into its statement blocks.

    The file's content before the first boundary marker is a comment
    preamble and is discarded; every segment after is one statement block,
    verbatim (unstripped) — matching how the notebook's own
    `BOOTSTRAP_STATEMENTS`/`MEMORY_MIGRATION_STATEMENTS`/
    `INVOICE_MEMORY_MIGRATION_STATEMENTS` Python tuples held each element.
    """

    content = sql_path.read_text(encoding="utf-8")
    segments = content.split(STATEMENT_BOUNDARY_MARKER)

    return tuple(segments[1:])


def canonical_migration_sql(statements: tuple[str, ...]) -> str:
    """Notebook's `calculate_migration_checksum`/`canonical_migration_sql`/
    `canonicalize_migration` canonicalization, verbatim (all three notebook
    functions are this same algorithm)."""

    return "\n".join(statement.strip() for statement in statements)


def calculate_migration_checksum(statements: tuple[str, ...]) -> str:
    return sha256(canonical_migration_sql(statements).encode("utf-8")).hexdigest()


def create_advisory_lock_key(lock_name: str) -> int:
    """Notebook `create_advisory_lock_key`, verbatim."""

    digest = sha256(lock_name.encode("utf-8")).digest()

    return int.from_bytes(digest[:8], byteorder="big", signed=True)


MIGRATION_LOCK_KEY = create_advisory_lock_key("ap_agent.schema_migrations")


def load_migration(entry: ManifestEntry) -> LoadedMigration:
    """Load one migration's SQL and verify it against the manifest.

    Raises `MigrationIntegrityError` *before* any database is touched if
    the file on disk no longer canonicalizes to the manifest's immutable
    `expected_checksum` (task §3.8).
    """

    sql_path = MIGRATIONS_DIRECTORY / entry.sql_filename
    statements = load_migration_statements(sql_path)
    checksum = calculate_migration_checksum(statements)

    if checksum != entry.expected_checksum:
        raise MigrationIntegrityError(
            f"{entry.migration_id}: the SQL file's checksum ({checksum}) no "
            f"longer matches the immutable manifest checksum "
            f"({entry.expected_checksum}). The migration file must not be "
            "edited after it has been applied.",
            details={"migration_id": entry.migration_id},
        )

    return LoadedMigration(
        migration_id=entry.migration_id,
        description=entry.description,
        statements=statements,
        checksum=checksum,
    )


def load_all_migrations() -> tuple[LoadedMigration, ...]:
    return tuple(load_migration(entry) for entry in MIGRATION_MANIFEST)


def apply_bootstrap_registry(
    cursor: "psycopg.Cursor",
    migration: LoadedMigration,
) -> dict[str, Any]:
    """Apply `0001_memory_schema_bootstrap`.

    Special-cased because this migration creates
    `ap_agent.schema_migrations` itself: there is nothing to `SELECT`
    against until its own statements have run. Mirrors
    `apply_bootstrap_migration` (notebook cell 85) verbatim.
    """

    cursor.execute("SELECT pg_advisory_xact_lock(%s);", (MIGRATION_LOCK_KEY,))

    for statement in migration.statements:
        cursor.execute(statement)

    cursor.execute(
        """
        SELECT checksum, description, applied_at, applied_by
        FROM ap_agent.schema_migrations
        WHERE migration_id = %s;
        """,
        (migration.migration_id,),
    )

    existing = cursor.fetchone()

    if existing is None:
        cursor.execute(
            """
            INSERT INTO ap_agent.schema_migrations
                (migration_id, checksum, description)
            VALUES (%s, %s, %s)
            RETURNING applied_at, applied_by;
            """,
            (migration.migration_id, migration.checksum, migration.description),
        )

        applied_at, applied_by = cursor.fetchone()

        return {
            "migration_id": migration.migration_id,
            "checksum": migration.checksum,
            "status": "APPLIED",
            "applied_at": applied_at,
            "applied_by": applied_by,
        }

    existing_checksum, existing_description, applied_at, applied_by = existing

    _verify_recorded_migration(migration, existing_checksum, existing_description)

    return {
        "migration_id": migration.migration_id,
        "checksum": migration.checksum,
        "status": "ALREADY_APPLIED",
        "applied_at": applied_at,
        "applied_by": applied_by,
    }


def _verify_recorded_migration(
    migration: LoadedMigration,
    existing_checksum: str,
    existing_description: str,
) -> None:
    if existing_checksum.strip() != migration.checksum:
        raise MigrationIntegrityError(
            f"Migration checksum mismatch for {migration.migration_id}. "
            "The recorded migration must not be silently modified.",
            details={"migration_id": migration.migration_id},
        )

    if existing_description != migration.description:
        raise MigrationIntegrityError(
            f"Migration description mismatch for {migration.migration_id}.",
            details={"migration_id": migration.migration_id},
        )


def apply_migration(
    cursor: "psycopg.Cursor",
    migration: LoadedMigration,
) -> dict[str, Any]:
    """Apply one non-bootstrap migration exactly once.

    Mirrors `apply_memory_schema_migration`/`apply_invoice_memory_migration`
    (notebook cells 86/88), which are the same algorithm applied to
    different SQL. Transactional (caller controls the connection/commit),
    concurrency-safe (`pg_advisory_xact_lock`), idempotent, fail-closed on
    checksum/description mismatch.
    """

    if migration.migration_id == BOOTSTRAP_MIGRATION_ID:
        return apply_bootstrap_registry(cursor, migration)

    cursor.execute("SELECT pg_advisory_xact_lock(%s);", (MIGRATION_LOCK_KEY,))

    cursor.execute(
        """
        SELECT checksum, description, applied_at, applied_by
        FROM ap_agent.schema_migrations
        WHERE migration_id = %s;
        """,
        (migration.migration_id,),
    )

    existing = cursor.fetchone()

    if existing is not None:
        existing_checksum, existing_description, applied_at, applied_by = existing

        _verify_recorded_migration(migration, existing_checksum, existing_description)

        return {
            "migration_id": migration.migration_id,
            "checksum": migration.checksum,
            "status": "ALREADY_APPLIED",
            "applied_at": applied_at,
            "applied_by": applied_by,
        }

    for statement in migration.statements:
        cursor.execute(statement)

    cursor.execute(
        """
        INSERT INTO ap_agent.schema_migrations
            (migration_id, checksum, description)
        VALUES (%s, %s, %s)
        RETURNING applied_at, applied_by;
        """,
        (migration.migration_id, migration.checksum, migration.description),
    )

    applied_at, applied_by = cursor.fetchone()

    return {
        "migration_id": migration.migration_id,
        "checksum": migration.checksum,
        "status": "APPLIED",
        "applied_at": applied_at,
        "applied_by": applied_by,
    }


def apply_all_migrations(dsn: str, config: MemoryConfig) -> tuple[dict[str, Any], ...]:
    """Apply every migration in `MIGRATION_MANIFEST`, in order, one
    connection/transaction per migration (matching the notebook's one
    `psycopg.connect(...)` per `apply_*_migration` call). Later, additive
    migrations (0002, 0003) never cause an earlier migration's validation
    to fail (task §4A) because each migration is checked and applied
    independently, in sequence.
    """

    results = []

    for migration in load_all_migrations():
        with open_connection(dsn, config) as connection:
            with connection.cursor() as cursor:
                results.append(apply_migration(cursor, migration))

    return tuple(results)


def inspect_schema_migrations(
    dsn: str,
    config: MemoryConfig,
) -> tuple[dict[str, Any], ...]:
    """Read back every row of `ap_agent.schema_migrations`, ordered by
    `applied_at`. Read-only; makes no schema changes."""

    with open_connection(dsn, config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT migration_id, checksum, description, applied_at, applied_by
                FROM ap_agent.schema_migrations
                ORDER BY applied_at;
                """
            )

            rows = cursor.fetchall()

    return tuple(
        {
            "migration_id": row[0],
            "checksum": row[1],
            "description": row[2],
            "applied_at": row[3],
            "applied_by": row[4],
        }
        for row in rows
    )

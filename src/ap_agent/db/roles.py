"""PostgreSQL role/grant management for the `ap_agent` schema.

New in this milestone's PostgreSQL-acceptance pass. Not sourced from the
notebook (Phase 7 never provisioned a runtime role — the notebook always
connected as `neondb_owner`); this is deployment/ops tooling shared by
`scripts/migrate.py` (real deployments) and
`tests/integration/test_memory_postgres_integration.py` (which must run
its RLS-sensitive tests through a dedicated, least-privilege, non-owner
role rather than the migration-owner connection — a superuser or
`BYPASSRLS` role would make every row-level-security assertion
meaningless, since RLS never applies to it).

Deliberately not part of `ap_agent.db.migration_runner`/the checksummed
migration manifest (task §3.8): granting privileges to an
environment-specific role name is re-runnable configuration, not
notebook-sourced schema content.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ap_agent.config.postgres import MemoryConfig
from ap_agent.db.connection import open_connection

if TYPE_CHECKING:  # pragma: no cover
    pass

__all__ = [
    "IDENTIFIER_PATTERN",
    "validate_identifier",
    "role_exists",
    "create_least_privilege_role",
    "grant_schema_access",
    "role_privilege_flags",
]

IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def validate_identifier(name: str, *, label: str) -> str:
    """Validate a SQL identifier (role or schema name) that must be
    interpolated into DDL text (PostgreSQL has no parameter placeholder
    for identifiers). Never accepts anything but `[a-zA-Z_][a-zA-Z0-9_]*`,
    so this is safe against injection regardless of where `name` came
    from (environment variable, CLI argument, test fixture)."""

    if not IDENTIFIER_PATTERN.fullmatch(name):
        raise ValueError(f"Invalid {label}: {name!r}")

    return name


def role_exists(dsn: str, config: MemoryConfig, *, role_name: str) -> bool:
    validate_identifier(role_name, label="role name")

    with open_connection(dsn, config) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s;", (role_name,))
            return cursor.fetchone() is not None


def create_least_privilege_role(
    dsn: str,
    config: MemoryConfig,
    *,
    role_name: str,
    password: str,
) -> None:
    """Create (idempotently) a `LOGIN` role with neither `SUPERUSER` nor
    `BYPASSRLS` (task: "Add an isolated test runtime role"), and no
    `CREATEDB`/`CREATEROLE`.

    `CREATE ROLE`/`ALTER ROLE` are PostgreSQL utility statements, not an
    ordinary parsed/planned query — the server does not accept a protocol
    bind parameter (`$1`) in the `PASSWORD` position, so
    `cursor.execute("...PASSWORD %s...", (password,))` fails with a
    `SyntaxError` before ever reaching the password. `password` is
    instead composed directly into the statement text via
    `psycopg.sql.Literal`, which quotes and escapes it the same way
    libpq's own literal-quoting rules would (verified against passwords
    containing quotes, backslashes, spaces and symbols in
    `tests/unit/test_memory_roles.py`) — never by hand, `repr()`, string
    concatenation or manual escaping. `role_name` is additionally composed
    via `psycopg.sql.Identifier` on top of (not instead of)
    `validate_identifier`'s regex allowlist below, so identifier safety
    does not rest on `sql.Identifier` alone. `password` is never logged.
    """

    from psycopg import sql

    validate_identifier(role_name, label="role name")

    with open_connection(dsn, config) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s;", (role_name,))

            if cursor.fetchone() is not None:
                # Idempotent: refresh the password (the caller generates a
                # fresh one per test session) and re-assert the
                # least-privilege flags rather than assuming a role left
                # over from an earlier run still has them.
                cursor.execute(
                    sql.SQL(
                        "ALTER ROLE {role} WITH LOGIN PASSWORD {password} "
                        "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;"
                    ).format(
                        role=sql.Identifier(role_name),
                        password=sql.Literal(password),
                    )
                )
                return

            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {role} WITH LOGIN PASSWORD {password} "
                    "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;"
                ).format(
                    role=sql.Identifier(role_name),
                    password=sql.Literal(password),
                )
            )


def grant_schema_access(
    dsn: str,
    config: MemoryConfig,
    *,
    role_name: str,
    privileges: tuple[str, ...] = ("SELECT", "INSERT", "UPDATE"),
) -> None:
    """Grant `role_name` `USAGE` on `config.schema_name` and `privileges`
    on every current and future table in it. `role_name`/`schema_name`
    are validated identifiers (see `validate_identifier`); `privileges`
    is never taken from unvalidated input by any caller in this codebase
    (both call sites pass a literal tuple).
    """

    validate_identifier(role_name, label="role name")
    schema = validate_identifier(config.schema_name, label="schema name")

    for privilege in privileges:
        if not re.fullmatch(r"[A-Z]+", privilege):
            raise ValueError(f"Invalid privilege: {privilege!r}")

    privilege_list = ", ".join(privileges)

    with open_connection(dsn, config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role_name};")
            cursor.execute(
                f"GRANT {privilege_list} ON ALL TABLES IN SCHEMA {schema} TO {role_name};"
            )
            cursor.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
                f"GRANT {privilege_list} ON TABLES TO {role_name};"
            )


def role_privilege_flags(dsn: str, config: MemoryConfig, *, role_name: str) -> dict:
    """Read back `rolsuper`/`rolbypassrls`/`rolcreatedb`/`rolcreaterole`
    for `role_name`, so a caller can assert the role really is
    least-privilege rather than assuming its own `CREATE ROLE` statement
    was honoured."""

    validate_identifier(role_name, label="role name")

    with open_connection(dsn, config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
                "FROM pg_roles WHERE rolname = %s;",
                (role_name,),
            )
            row = cursor.fetchone()

    if row is None:
        raise ValueError(f"Role {role_name!r} does not exist.")

    return {
        "superuser": row[0],
        "bypassrls": row[1],
        "createdb": row[2],
        "createrole": row[3],
    }

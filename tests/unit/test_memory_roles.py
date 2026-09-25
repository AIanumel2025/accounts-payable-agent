"""M8D unit tests: `ap_agent.db.roles`.

Identifier validation is pure-function and tested directly. The
`CREATE ROLE`/`ALTER ROLE` password-composition tests exercise
`create_least_privilege_role` against a fake connection/cursor (no
database) so the exact `psycopg.sql.Composed` statement it builds can be
inspected without a live PostgreSQL instance — real execution of this
module's SQL-touching functions is covered by
`tests/integration/test_memory_postgres_integration.py`.

Regression coverage for the bug fixed in this pass: `CREATE ROLE`/
`ALTER ROLE` are PostgreSQL utility statements and do not accept a
protocol bind parameter (`$1`) in the `PASSWORD` position —
`cursor.execute("...PASSWORD %s...", (password,))` fails with
`psycopg.errors.SyntaxError: syntax error at or near "$1"` before ever
reaching the password. The fix composes the password with
`psycopg.sql.Literal` (client-side quoting/escaping, the same safety
property a bind parameter would give) and the role name with
`psycopg.sql.Identifier`, on top of (not instead of) the existing
`validate_identifier` regex allowlist.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from ap_agent.config.postgres import MemoryConfig
from ap_agent.db import roles
from ap_agent.db.roles import validate_identifier

pytestmark = pytest.mark.unit

ROLE_NAME = "ap_agent_m8_test_runtime"
TRICKY_PASSWORDS = [
    "plain-ascii-password123",
    "has spaces in it",
    "quote's-and-\"quotes\"",
    "back\\slash\\heavy\\value",
    "symbols!@#$%^&*()_+-=[]{}|;:,.<>?",
    "'; DROP ROLE ap_agent_m8_test_runtime; --",
    "",
]


class _FakeCursor:
    """Captures every `execute(query, params)` call; `fetchone` reports
    whether the role already exists on the *first* call only (the
    `SELECT 1 FROM pg_roles ...` existence check), matching
    `create_least_privilege_role`'s own call order."""

    def __init__(self, *, role_exists: bool) -> None:
        self.role_exists = role_exists
        self.calls: list[tuple[object, object]] = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return self

    def fetchone(self):
        if len(self.calls) == 1:
            return (1,) if self.role_exists else None
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _patch_open_connection(monkeypatch, cursor: _FakeCursor) -> None:
    @contextmanager
    def _fake_open_connection(dsn, config):
        yield _FakeConnection(cursor)

    monkeypatch.setattr(roles, "open_connection", _fake_open_connection)


def _ddl_call(cursor: _FakeCursor):
    """The second `execute()` call is the `CREATE ROLE`/`ALTER ROLE`
    statement; the first is the existence-check `SELECT`."""

    assert len(cursor.calls) == 2
    query, params = cursor.calls[1]
    return query, params


# ------------------------------------------------------------
# Identifier validation (pure function)
# ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["ap_agent_app", "_leading_underscore", "Role1", "a", "role_with_numbers123"],
)
def test_validate_identifier_accepts_safe_names(name):
    assert validate_identifier(name, label="role name") == name


@pytest.mark.parametrize(
    "name",
    [
        "role; DROP TABLE ap_agent.tenants;--",
        "role name",
        "role-name",
        "1role",
        "",
        "role'name",
        "role\"name",
        "role.name",
    ],
)
def test_validate_identifier_rejects_unsafe_names(name):
    with pytest.raises(ValueError):
        validate_identifier(name, label="role name")


def test_create_least_privilege_role_rejects_unsafe_identifier_before_any_sql(
    monkeypatch,
):
    cursor = _FakeCursor(role_exists=False)
    _patch_open_connection(monkeypatch, cursor)

    with pytest.raises(ValueError):
        roles.create_least_privilege_role(
            "postgresql://example/db",
            MemoryConfig(),
            role_name="role; DROP TABLE ap_agent.tenants;--",
            password="irrelevant",
        )

    assert cursor.calls == []


# ------------------------------------------------------------
# CREATE ROLE / ALTER ROLE composition
# ------------------------------------------------------------


def test_create_role_uses_sql_composition_not_a_bind_parameter(monkeypatch):
    cursor = _FakeCursor(role_exists=False)
    _patch_open_connection(monkeypatch, cursor)

    roles.create_least_privilege_role(
        "postgresql://example/db",
        MemoryConfig(),
        role_name=ROLE_NAME,
        password="hunter2-password",
    )

    query, params = _ddl_call(cursor)

    # A real bind parameter would appear as a second `execute()` argument;
    # the fix composes the whole statement client-side instead, so no
    # params tuple is passed for the DDL statement (this is exactly what
    # the original bug's `params = ('...',)` traceback looked like).
    assert params is None
    rendered = query.as_string(None)
    assert rendered.startswith("CREATE ROLE ")
    assert f'"{ROLE_NAME}"' in rendered
    assert "hunter2-password" in rendered
    assert "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE" in rendered
    # No literal `%s`/`$1` placeholder should ever reach the server.
    assert "%s" not in rendered
    assert "$1" not in rendered


def test_alter_role_uses_sql_composition_for_an_existing_role(monkeypatch):
    cursor = _FakeCursor(role_exists=True)
    _patch_open_connection(monkeypatch, cursor)

    roles.create_least_privilege_role(
        "postgresql://example/db",
        MemoryConfig(),
        role_name=ROLE_NAME,
        password="a-second-password",
    )

    query, params = _ddl_call(cursor)

    assert params is None
    rendered = query.as_string(None)
    assert rendered.startswith("ALTER ROLE ")
    assert f'"{ROLE_NAME}"' in rendered
    assert "a-second-password" in rendered
    assert "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE" in rendered
    assert "%s" not in rendered
    assert "$1" not in rendered


@pytest.mark.parametrize("password", TRICKY_PASSWORDS)
@pytest.mark.parametrize("role_exists", [False, True], ids=["create", "alter"])
def test_password_composition_handles_quotes_spaces_backslashes_and_symbols(
    monkeypatch, password, role_exists
):
    cursor = _FakeCursor(role_exists=role_exists)
    _patch_open_connection(monkeypatch, cursor)

    roles.create_least_privilege_role(
        "postgresql://example/db",
        MemoryConfig(),
        role_name=ROLE_NAME,
        password=password,
    )

    query, params = _ddl_call(cursor)
    assert params is None

    # `sql.Literal` is libpq-grade quoting: rendering must not raise, and
    # the composed text must remain a single well-formed statement — a
    # naive f-string/`%`-interpolation of an unescaped quote or backslash
    # would instead corrupt the statement (early-terminate the string
    # literal, or turn a fragment of the password into SQL keywords).
    rendered = query.as_string(None)
    assert rendered.count("PASSWORD") == 1
    assert rendered.rstrip().endswith(
        "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;"
    )


def test_least_privilege_flags_present_on_create_and_alter(monkeypatch):
    for role_exists in (False, True):
        cursor = _FakeCursor(role_exists=role_exists)
        _patch_open_connection(monkeypatch, cursor)

        roles.create_least_privilege_role(
            "postgresql://example/db",
            MemoryConfig(),
            role_name=ROLE_NAME,
            password="whatever-password",
        )

        query, _ = _ddl_call(cursor)
        rendered = query.as_string(None)

        assert "LOGIN" in rendered
        assert rendered.rstrip().endswith(
            "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;"
        )


# ------------------------------------------------------------
# No credential ever printed/logged
# ------------------------------------------------------------


@pytest.mark.parametrize("role_exists", [False, True], ids=["create", "alter"])
def test_create_least_privilege_role_never_prints_the_password(
    monkeypatch, capsys, role_exists
):
    cursor = _FakeCursor(role_exists=role_exists)
    _patch_open_connection(monkeypatch, cursor)
    secret_password = "do-not-print-me-ever-9f3c"

    roles.create_least_privilege_role(
        "postgresql://example/db",
        MemoryConfig(),
        role_name=ROLE_NAME,
        password=secret_password,
    )

    captured = capsys.readouterr()
    assert secret_password not in captured.out
    assert secret_password not in captured.err

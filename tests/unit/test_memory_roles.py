"""M8D/M9G unit tests: `ap_agent.db.roles`.

Identifier validation is pure-function and tested directly. The
`CREATE ROLE`/`ALTER ROLE` password-composition tests exercise
`create_least_privilege_role` against a fake connection/cursor (no
database) so the exact `psycopg.sql.Composed` statement it builds can be
inspected without a live PostgreSQL instance — real execution of this
module's SQL-touching functions is covered by
`tests/integration/test_memory_postgres_integration.py`.

Regression coverage for the bug fixed in M8: `CREATE ROLE`/`ALTER ROLE`
are PostgreSQL utility statements and do not accept a protocol bind
parameter (`$1`) in the `PASSWORD` position —
`cursor.execute("...PASSWORD %s...", (password,))` fails with
`psycopg.errors.SyntaxError: syntax error at or near "$1"` before ever
reaching the password. The fix composes the password with
`psycopg.sql.Literal` (client-side quoting/escaping, the same safety
property a bind parameter would give) and the role name with
`psycopg.sql.Identifier`, on top of (not instead of) the existing
`validate_identifier` regex allowlist.

Regression coverage for the bug fixed in M9 (task brief: "M9 hotfix —
managed-PostgreSQL role idempotency"): on a managed PostgreSQL instance
(Neon), the DSN owner role is not itself a superuser, so
`ALTER ROLE ... NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE` against an
*already-existing* runtime role (left over from an earlier CI run against
the same persistent test database) fails with
`psycopg.errors.InsufficientPrivilege: permission denied to alter role ...
Only roles with the SUPERUSER attribute may change the SUPERUSER
attribute.` — PostgreSQL requires the *executing* role to itself hold
`SUPERUSER`/`BYPASSRLS` to change either attribute on *any* role, even to
reassert its current (already-`NO*`) value. The fix reads the existing
role's actual privilege flags from `pg_roles` first: if the role is
already least-privilege, only its password/`LOGIN` are refreshed (an
`ALTER ROLE` that never mentions `SUPERUSER`/`BYPASSRLS`/`CREATEDB`/
`CREATEROLE` at all); if it is unexpectedly privileged, the function fails
closed with `PrivilegedRoleError` instead of attempting (and possibly
failing, or silently no-op'ing) a restricted `ALTER`.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from ap_agent.config.postgres import MemoryConfig
from ap_agent.db import roles
from ap_agent.db.roles import validate_identifier
from ap_agent.exceptions import PrivilegedRoleError

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

# (superuser, bypassrls, createdb, createrole)
LEAST_PRIVILEGE_ROW = (False, False, False, False)


class _FakeCursor:
    """Captures every `execute(query, params)` call.

    `existing_flags_row` models the *first* `SELECT ... FROM pg_roles`
    call (existence + privilege-flag check, in one query — see
    `create_least_privilege_role`): `None` means the role does not exist
    yet; a 4-tuple `(rolsuper, rolbypassrls, rolcreatedb, rolcreaterole)`
    means it does. When the role does not exist, the *second* `SELECT`
    (the post-`CREATE ROLE` verification read-back) returns
    `verify_row_after_create` (defaults to the least-privilege row, i.e. a
    verification that passes).
    """

    def __init__(
        self,
        *,
        existing_flags_row: tuple | None,
        verify_row_after_create: tuple | None = LEAST_PRIVILEGE_ROW,
    ) -> None:
        self.existing_flags_row = existing_flags_row
        self.verify_row_after_create = verify_row_after_create
        self.calls: list[tuple[object, object]] = []
        self._select_count = 0

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return self

    def fetchone(self):
        rendered = self._rendered_last_query()

        if rendered.startswith("SELECT"):
            self._select_count += 1

            if self._select_count == 1:
                return self.existing_flags_row

            return self.verify_row_after_create

        return None

    def _rendered_last_query(self) -> str:
        query, _params = self.calls[-1]
        if isinstance(query, str):
            return query
        return query.as_string(None)

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


def _create_cursor(monkeypatch, *, role_exists: bool, existing_flags: tuple = LEAST_PRIVILEGE_ROW) -> _FakeCursor:
    cursor = _FakeCursor(existing_flags_row=existing_flags if role_exists else None)
    _patch_open_connection(monkeypatch, cursor)
    return cursor


def _ddl_call(cursor: _FakeCursor):
    """The second `execute()` call is the `CREATE ROLE`/`ALTER ROLE`
    statement; the first is the existence-and-flags `SELECT`."""

    assert len(cursor.calls) >= 2
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
    cursor = _create_cursor(monkeypatch, role_exists=False)

    with pytest.raises(ValueError):
        roles.create_least_privilege_role(
            "postgresql://example/db",
            MemoryConfig(),
            role_name="role; DROP TABLE ap_agent.tenants;--",
            password="irrelevant",
        )

    assert cursor.calls == []


# ------------------------------------------------------------
# First execution: role does not exist yet (CREATE ROLE)
# ------------------------------------------------------------


def test_create_role_uses_sql_composition_not_a_bind_parameter(monkeypatch):
    cursor = _create_cursor(monkeypatch, role_exists=False)

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


def test_create_role_verifies_flags_after_creation(monkeypatch):
    """A third call (the post-CREATE verification SELECT) must happen,
    and must read from `pg_roles` again rather than assuming success."""

    cursor = _create_cursor(monkeypatch, role_exists=False)

    roles.create_least_privilege_role(
        "postgresql://example/db",
        MemoryConfig(),
        role_name=ROLE_NAME,
        password="hunter2-password",
    )

    assert len(cursor.calls) == 3
    verify_query, verify_params = cursor.calls[2]
    assert verify_query.upper().startswith("SELECT")
    assert verify_params == (ROLE_NAME,)


def test_create_role_fails_closed_when_verification_shows_an_elevated_flag(monkeypatch):
    """If the just-created role somehow reads back as privileged (a
    defensive check; should not happen against a well-behaved server),
    the function must fail closed rather than return successfully."""

    cursor = _FakeCursor(
        existing_flags_row=None,
        verify_row_after_create=(True, False, False, False),
    )
    _patch_open_connection(monkeypatch, cursor)

    with pytest.raises(PrivilegedRoleError, match="superuser"):
        roles.create_least_privilege_role(
            "postgresql://example/db",
            MemoryConfig(),
            role_name=ROLE_NAME,
            password="hunter2-password",
        )


# ------------------------------------------------------------
# Second execution: role already exists (idempotent ALTER path)
# ------------------------------------------------------------


def test_alter_role_uses_sql_composition_for_an_existing_role(monkeypatch):
    cursor = _create_cursor(monkeypatch, role_exists=True)

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
    assert "LOGIN" in rendered
    assert "%s" not in rendered
    assert "$1" not in rendered

    # The M9 fix: an existing role's ALTER must never re-specify any of
    # the four restricted/elevated attributes -- touching SUPERUSER/
    # BYPASSRLS at all requires the *executing* role to already hold that
    # attribute, on managed PostgreSQL (Neon), regardless of the target
    # value.
    assert "SUPERUSER" not in rendered.upper()
    assert "BYPASSRLS" not in rendered.upper()
    assert "CREATEDB" not in rendered.upper()
    assert "CREATEROLE" not in rendered.upper()

    # Exactly two statements for the existing-role path: the flags SELECT,
    # then the ALTER. No verification re-read is needed afterward, since
    # ALTER never touched the four attributes this function cares about.
    assert len(cursor.calls) == 2


def test_alter_role_rotates_the_password_on_the_existing_role_path(monkeypatch):
    cursor = _create_cursor(monkeypatch, role_exists=True)

    roles.create_least_privilege_role(
        "postgresql://example/db",
        MemoryConfig(),
        role_name=ROLE_NAME,
        password="brand-new-rotated-password",
    )

    _, params = _ddl_call(cursor)
    query, _ = _ddl_call(cursor)
    rendered = query.as_string(None)

    assert params is None
    assert "brand-new-rotated-password" in rendered


def test_an_existing_correctly_restricted_role_is_reused_without_error(monkeypatch):
    """The common CI case this fix targets: the runtime role from a
    previous, successful run already exists and is already
    least-privilege. This must succeed (not raise), refreshing only the
    password."""

    cursor = _create_cursor(monkeypatch, role_exists=True, existing_flags=LEAST_PRIVILEGE_ROW)

    roles.create_least_privilege_role(
        "postgresql://example/db",
        MemoryConfig(),
        role_name=ROLE_NAME,
        password="whatever-password",
    )

    query, _ = _ddl_call(cursor)
    assert query.as_string(None).startswith("ALTER ROLE ")


# ------------------------------------------------------------
# Fail-closed rejection of an unexpectedly privileged existing role
# ------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "expected_label"),
    [
        ((True, False, False, False), "superuser"),
        ((False, True, False, False), "bypassrls"),
        ((False, False, True, False), "createdb"),
        ((False, False, False, True), "createrole"),
    ],
    ids=["superuser", "bypassrls", "createdb", "createrole"],
)
def test_fails_closed_when_existing_role_has_an_elevated_attribute(monkeypatch, flags, expected_label):
    cursor = _create_cursor(monkeypatch, role_exists=True, existing_flags=flags)

    with pytest.raises(PrivilegedRoleError, match=expected_label):
        roles.create_least_privilege_role(
            "postgresql://example/db",
            MemoryConfig(),
            role_name=ROLE_NAME,
            password="whatever-password",
        )

    # Fail closed *before* issuing any ALTER: only the flags SELECT ran.
    assert len(cursor.calls) == 1


def test_fails_closed_without_attempting_to_downgrade_the_privileged_role(monkeypatch):
    """The defect this fix replaces: blindly issuing `ALTER ROLE ...
    NOSUPERUSER ...` against a role the caller cannot administer. The
    corrected function must never even attempt that statement."""

    cursor = _create_cursor(monkeypatch, role_exists=True, existing_flags=(True, True, False, False))

    with pytest.raises(PrivilegedRoleError):
        roles.create_least_privilege_role(
            "postgresql://example/db",
            MemoryConfig(),
            role_name=ROLE_NAME,
            password="whatever-password",
        )

    rendered_statements = [
        (q if isinstance(q, str) else q.as_string(None)) for q, _ in cursor.calls
    ]
    assert not any("ALTER ROLE" in statement for statement in rendered_statements)
    assert not any("SUPERUSER" in statement.upper() and "SELECT" not in statement.upper() for statement in rendered_statements)


def test_privileged_role_error_names_every_elevated_attribute(monkeypatch):
    cursor = _create_cursor(monkeypatch, role_exists=True, existing_flags=(True, True, True, True))

    with pytest.raises(PrivilegedRoleError) as excinfo:
        roles.create_least_privilege_role(
            "postgresql://example/db",
            MemoryConfig(),
            role_name=ROLE_NAME,
            password="whatever-password",
        )

    message = str(excinfo.value)
    for label in ("superuser", "bypassrls", "createdb", "createrole"):
        assert label in message

    assert excinfo.value.details["role_name"] == ROLE_NAME
    assert set(excinfo.value.details["elevated_attributes"]) == {
        "superuser",
        "bypassrls",
        "createdb",
        "createrole",
    }


# ------------------------------------------------------------
# SQL-injection-shaped role names and passwords
# ------------------------------------------------------------


@pytest.mark.parametrize(
    "malicious_role_name",
    [
        "ap_agent_m8_test_runtime; DROP ROLE ap_agent_m8_test_runtime; --",
        "ap_agent_m8_test_runtime\"; DROP TABLE ap_agent.tenants; --",
        "ap_agent_m8_test_runtime' OR '1'='1",
        "ap_agent_m8_test_runtime\\",
    ],
)
@pytest.mark.parametrize("role_exists", [False, True], ids=["create", "alter"])
def test_sql_injection_shaped_role_names_are_rejected_before_any_sql(
    monkeypatch, malicious_role_name, role_exists
):
    cursor = _create_cursor(monkeypatch, role_exists=role_exists)

    with pytest.raises(ValueError):
        roles.create_least_privilege_role(
            "postgresql://example/db",
            MemoryConfig(),
            role_name=malicious_role_name,
            password="irrelevant",
        )

    # validate_identifier fails closed before any query is issued at all.
    assert cursor.calls == []


@pytest.mark.parametrize(
    "malicious_password",
    [
        "'; DROP ROLE ap_agent_m8_test_runtime; --",
        "\" OR \"\"=\"",
        "pw' UNION SELECT rolpassword FROM pg_authid --",
        "\\'; --",
    ],
)
@pytest.mark.parametrize("role_exists", [False, True], ids=["create", "alter"])
def test_sql_injection_shaped_passwords_are_safely_quoted_not_executed_as_sql(
    monkeypatch, malicious_password, role_exists
):
    from psycopg import sql

    cursor = _create_cursor(monkeypatch, role_exists=role_exists)

    roles.create_least_privilege_role(
        "postgresql://example/db",
        MemoryConfig(),
        role_name=ROLE_NAME,
        password=malicious_password,
    )

    query, params = _ddl_call(cursor)
    assert params is None

    # A raw semicolon count is not meaningful here: a safely quoted literal
    # is free to *contain* semicolon characters (they are just bytes inside
    # a string, not statement separators) -- this password's whole point is
    # to contain one. What must hold is that the literal renders exactly as
    # psycopg's own trusted `sql.Literal` quoting would (proving the
    # password was never concatenated/escaped by hand), and that the
    # statement's only *unquoted* semicolon is the single trailing
    # terminator.
    expected_literal = sql.Literal(malicious_password).as_string(None)
    rendered = query.as_string(None)

    assert expected_literal in rendered
    assert rendered.count(expected_literal) == 1
    assert rendered.count("PASSWORD") == 1

    remainder = rendered.replace(expected_literal, "", 1)
    assert remainder.count(";") == 1
    assert remainder.rstrip().endswith(";")


@pytest.mark.parametrize("password", TRICKY_PASSWORDS)
@pytest.mark.parametrize("role_exists", [False, True], ids=["create", "alter"])
def test_password_composition_handles_quotes_spaces_backslashes_and_symbols(
    monkeypatch, password, role_exists
):
    cursor = _create_cursor(monkeypatch, role_exists=role_exists)

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


def test_least_privilege_flags_present_on_create(monkeypatch):
    cursor = _create_cursor(monkeypatch, role_exists=False)

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


def test_login_present_on_alter_for_an_existing_role(monkeypatch):
    cursor = _create_cursor(monkeypatch, role_exists=True)

    roles.create_least_privilege_role(
        "postgresql://example/db",
        MemoryConfig(),
        role_name=ROLE_NAME,
        password="whatever-password",
    )

    query, _ = _ddl_call(cursor)
    rendered = query.as_string(None)

    assert "LOGIN" in rendered
    assert rendered.rstrip().endswith(";")


# ------------------------------------------------------------
# No credential ever printed/logged
# ------------------------------------------------------------


@pytest.mark.parametrize("role_exists", [False, True], ids=["create", "alter"])
def test_create_least_privilege_role_never_prints_the_password(
    monkeypatch, capsys, role_exists
):
    cursor = _create_cursor(monkeypatch, role_exists=role_exists)
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


def test_privileged_role_error_never_includes_the_password(monkeypatch):
    """The failure path (an unexpectedly privileged existing role) must
    not leak the freshly generated password either, even though the
    caller passed one in."""

    cursor = _create_cursor(monkeypatch, role_exists=True, existing_flags=(True, False, False, False))
    secret_password = "do-not-leak-me-either-9f3c"

    with pytest.raises(PrivilegedRoleError) as excinfo:
        roles.create_least_privilege_role(
            "postgresql://example/db",
            MemoryConfig(),
            role_name=ROLE_NAME,
            password=secret_password,
        )

    assert secret_password not in str(excinfo.value)
    assert secret_password not in repr(excinfo.value.details)

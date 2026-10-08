"""M11E.3 (real AWS finding): a Neon transaction-pooled endpoint rejects startup options.

    psycopg.OperationalError: ERROR: unsupported startup parameter in options: statement_timeout.
    Please use unpooled connection or remove this parameter from the startup package.

`open_connection` always sent `options="-c statement_timeout=... -c lock_timeout=..."`. These tests emulate the pooler's
rejection (no database, no network) and pin the corrected behaviour: pooled endpoints get no startup options and receive the
limits transaction-locally, verified, never at session level; direct endpoints are unchanged. Real-database coverage (RLS,
no leakage) is in `tests/api/test_m11e3_pooled_postgres.py`."""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
import pytest

from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy
from ap_agent.db import connection as db
from ap_agent.exceptions import PostgresConfigurationError

pytestmark = pytest.mark.unit

POOLED = "postgresql://ap_agent_app:x@ep-example-pooler.region.aws.neon.tech/ap_agent_m8_test?sslmode=require&channel_binding=require"
DIRECT = "postgresql://owner:x@ep-example.region.aws.neon.tech/ap_agent_m8_test?sslmode=require&channel_binding=require"
POOLER_MESSAGE = (
    "ERROR: unsupported startup parameter in options: statement_timeout. "
    "Please use unpooled connection or remove this parameter from the startup package."
)


def _config(**changes) -> MemoryConfig:
    return MemoryConfig(
        transport_policy=PostgresTransportPolicy(require_ssl=True, require_channel_binding=True, require_pooled_endpoint=False), **changes
    )


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.connection.executed.append((sql, params))
        text = " ".join(sql.split()).lower()
        self.connection.in_transaction = True

        if text.startswith("select set_config"):
            name, value = params
            assert "true" in text  # transaction-local
            self.connection.local[name] = str(value)
            self._rows = [(value,)]
        elif "from pg_settings" in text:
            self._rows = [(name, self.connection.local.get(name, "0")) for name in params[0]]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConnection:
    """A connection with PostgreSQL's transaction-local setting semantics: values vanish when the transaction ends."""

    def __init__(self, kwargs, tamper=False):
        self.kwargs = kwargs
        self.executed: list = []
        self.local: dict[str, str] = {}
        self.in_transaction = False
        self.tamper = tamper
        self.ended = None

    def cursor(self):
        return FakeCursor(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *rest):
        self.ended = "rollback" if exc_type else "commit"
        self.local.clear()  # transaction-local settings end with the transaction
        return False


@pytest.fixture()
def neon(monkeypatch):
    """`psycopg.connect` emulating Neon: pooled hosts reject startup options, direct hosts accept them."""

    connections: list[FakeConnection] = []

    def connect(dsn, **kwargs):
        host = psycopg.conninfo.conninfo_to_dict(dsn)["host"]

        if "-pooler" in host and "options" in kwargs:
            raise psycopg.OperationalError(POOLER_MESSAGE)

        connection = FakeConnection(kwargs)

        if "options" in kwargs:  # direct endpoint: the server applies the startup options for the whole session
            for part in re.findall(r"-c (\w+)=(\d+)", kwargs["options"]):
                connection.local[part[0]] = part[1]

        connections.append(connection)
        return connection

    monkeypatch.setattr(psycopg, "connect", connect)
    return connections


def _legacy_open_connection(dsn, config):
    """The pre-M11E.3 implementation, verbatim: always sends the startup options."""

    return psycopg.connect(
        dsn, connect_timeout=config.connect_timeout_seconds, prepare_threshold=None,
        application_name=config.application_name, options=db.connection_options(config),
    )


# -- the defect ------------------------------------------------------------------------------------------------------------


def test_the_old_implementation_reproduces_the_neon_rejection(neon):
    with pytest.raises(psycopg.OperationalError, match="unsupported startup parameter in options: statement_timeout"):
        _legacy_open_connection(POOLED, _config())

    assert neon == []  # nothing connected


def test_the_old_implementation_is_fine_on_a_direct_endpoint(neon):
    assert _legacy_open_connection(DIRECT, _config()) is neon[0]


def test_the_fixed_open_connection_works_against_the_pooler(neon):
    with db.open_connection(POOLED, _config()) as connection:
        assert connection is neon[0]


# -- pooled: no startup options, transaction-local verified limits -------------------------------------------------------------


def test_a_pooled_endpoint_receives_no_startup_options(neon):
    with db.open_connection(POOLED, _config()):
        pass

    assert "options" not in neon[0].kwargs
    assert neon[0].kwargs["prepare_threshold"] is None and neon[0].kwargs["application_name"]  # the rest is unchanged
    assert db.startup_options(POOLED, _config()) is None and db.is_pooled_endpoint(POOLED)


def test_a_direct_endpoint_keeps_the_existing_startup_options(neon):
    config = _config()

    with db.open_connection(DIRECT, config) as connection:
        assert connection.kwargs["options"] == "-c statement_timeout=30000 -c lock_timeout=10000"
        assert connection.executed == []  # nothing extra is executed: behaviour unchanged

    assert db.startup_options(DIRECT, config) == db.connection_options(config) and not db.is_pooled_endpoint(DIRECT)


def test_the_timeouts_follow_the_configuration(neon):
    with db.open_connection(DIRECT, _config(statement_timeout_milliseconds=1234, lock_timeout_milliseconds=567)) as connection:
        assert connection.kwargs["options"] == "-c statement_timeout=1234 -c lock_timeout=567"

    with db.open_connection(POOLED, _config(statement_timeout_milliseconds=1234, lock_timeout_milliseconds=567)) as connection:
        assert connection.local == {"statement_timeout": "1234", "lock_timeout": "567"}


def test_a_pooled_open_connection_applies_both_limits_transaction_locally_before_yielding(neon):
    with db.open_connection(POOLED, _config()) as connection:
        # active inside the yielded transaction ...
        assert connection.local == {"statement_timeout": "30000", "lock_timeout": "10000"}
        statements = [" ".join(sql.split()) for sql, _ in connection.executed]
        assert statements[:2] == ["SELECT set_config(%s, %s, TRUE);"] * 2  # parameterised and transaction-local (TRUE)
        assert [params for sql, params in connection.executed[:2]] == [("statement_timeout", "30000"), ("lock_timeout", "10000")]
        assert "pg_settings" in statements[2]  # ... and verified by reading the effective values back

    assert neon[0].ended == "commit" and neon[0].local == {}  # gone with the transaction


def test_the_values_are_never_interpolated_into_the_sql(neon):
    with db.open_connection(POOLED, _config(statement_timeout_milliseconds=4321)) as connection:
        for sql, _ in connection.executed:
            assert "4321" not in sql and "30000" not in sql


def test_the_timeouts_cannot_leak_between_transactions_or_tenants(neon):
    with db.open_connection(POOLED, _config()) as first:
        assert first.local

    with db.open_connection(POOLED, _config(statement_timeout_milliseconds=111, lock_timeout_milliseconds=222)) as second:
        assert second.local == {"statement_timeout": "111", "lock_timeout": "222"}  # its own values, none inherited

    assert first.local == {} and second.local == {}

    for connection in neon:  # never a session-level setting: no SET, no set_config(..., false)
        for sql, _ in connection.executed:
            text = " ".join(sql.split()).lower()
            assert not text.startswith("set ") and "set session" not in text and "set_config(%s, %s, false)" not in text
            assert text.count("true") == (1 if text.startswith("select set_config") else 0)


def test_a_failed_transaction_discards_the_limits_too(neon):
    with pytest.raises(RuntimeError):
        with db.open_connection(POOLED, _config()) as connection:
            raise RuntimeError("boom")

    assert connection.ended == "rollback" and connection.local == {}


def test_unverified_limits_fail_closed(neon, monkeypatch):
    original = FakeCursor.execute

    def lying(self, sql, params=None):
        original(self, sql, params)

        if "from pg_settings" in " ".join(sql.split()).lower():
            self._rows = [("statement_timeout", "0"), ("lock_timeout", "0")]  # the server did not apply them

    monkeypatch.setattr(FakeCursor, "execute", lying)

    with pytest.raises(PostgresConfigurationError, match="not applied"):
        with db.open_connection(POOLED, _config()):
            pytest.fail("the connection must not be yielded")


def test_the_transport_policy_is_unchanged_and_still_requires_the_pooled_endpoint(neon):
    strict = MemoryConfig(transport_policy=PostgresTransportPolicy(require_ssl=True, require_channel_binding=True, require_pooled_endpoint=True))

    with db.open_connection(POOLED, strict):
        pass

    with pytest.raises(PostgresConfigurationError, match="not a pooled endpoint"):
        with db.open_connection(DIRECT, strict):
            pass

    with pytest.raises(PostgresConfigurationError, match="does not require SSL"):
        with db.open_connection(POOLED.replace("sslmode=require", "sslmode=prefer"), strict):
            pass


# -- the client-side pool --------------------------------------------------------------------------------------------------------


def test_a_client_side_pool_is_refused_on_a_pooled_endpoint():
    with pytest.raises(PostgresConfigurationError, match="not supported on a transaction-pooled endpoint"):
        db.create_connection_pool(POOLED, _config())


def test_a_client_side_pool_on_a_direct_endpoint_is_unchanged():
    pool = db.create_connection_pool(DIRECT, _config())

    try:
        assert pool.kwargs["options"] == "-c statement_timeout=30000 -c lock_timeout=10000" and pool.kwargs["prepare_threshold"] is None
    finally:
        pool.close()


def test_the_pool_still_checks_the_transport_policy_first():
    with pytest.raises(PostgresConfigurationError, match="does not require SSL"):
        db.create_connection_pool(POOLED.replace("sslmode=require", "sslmode=prefer"), _config())


def test_the_aws_deployment_keeps_the_client_side_pool_disabled():
    template = (Path(__file__).resolve().parents[2] / "deploy" / "aws" / "template.yaml").read_text()

    assert 'AP_AGENT_API_DISABLE_CONNECTION_POOL: "true"' in template


# -- every database call path goes through open_connection, one connection = one transaction ----------------------------------------


SRC = Path(__file__).resolve().parents[2] / "src"


def test_no_code_opens_its_own_connection_or_ends_a_transaction_mid_connection():
    """The transaction-local limits cover a connection's single transaction, so no caller may commit/rollback mid-connection,
    switch to autocommit or open a connection around `open_connection`."""

    offenders = []

    for path in SRC.rglob("*.py"):
        if path.name == "connection.py" and path.parent.name == "db":
            continue

        for number, line in enumerate(path.read_text().splitlines(), start=1):
            code = line.split("#", 1)[0]

            if re.search(r"(?<!\w)psycopg\.connect\(|\.autocommit|\.commit\(\)|\.rollback\(\)|(connection|conn)\.transaction\(", code) and '"""' not in code and "`" not in code:
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")

    assert offenders == []


@pytest.mark.parametrize(
    "module",
    [
        "ap_agent/auth/identity.py",  # Clerk identity resolution
        "ap_agent/auth/admin.py",  # tenant / identity administration
        "ap_agent/repositories/postgres_memory_repository.py",  # tenant registration, memory
        "ap_agent/repositories/review_repository.py",  # dashboard / review repositories
        "ap_agent/repositories/operations_repository.py",  # operations API and the OCR worker (transaction / worker_transaction)
        "ap_agent/api/routes/health.py",  # readiness probe
        "ap_agent/db/migration_runner.py",
    ],
)
def test_every_database_module_connects_only_through_open_connection(module):
    text = (SRC / module).read_text()

    assert "open_connection" in text and "psycopg.connect(" not in text.replace("`psycopg.connect(...)`", "")


def test_the_fargate_worker_has_no_database_connection_code_of_its_own():
    # The OCR task (`ap_agent.worker.*`) reaches PostgreSQL only through OperationsRepository, i.e. open_connection.
    for path in (SRC / "ap_agent" / "worker").glob("*.py"):
        assert not re.search(r"^\s*(import|from)\s+psycopg", path.read_text(), re.M), path.name

    assert "OperationsRepository" in (SRC / "ap_agent" / "worker" / "config.py").read_text() + (SRC / "ap_agent" / "worker" / "__main__.py").read_text()

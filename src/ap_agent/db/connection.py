"""PostgreSQL connection management for Phase 7 operational memory.

Source: notebook cell 84 ("PHASE 7 — CELL 2"), `load_postgres_dsn`,
`inspect_transport_policy`, `verify_postgres_connection`; and cell 83's
`memory_utc_now`/`postgres_dsn_is_configured`. Generalized per M8 task
brief §4:

- DSN loading no longer goes through Colab `userdata`
  (`load_postgres_dsn`); it reads the environment variable named by
  `MemoryConfig.dsn_environment_variable` /
  `MemoryConfig.migration_dsn_environment_variable` instead, and only when
  called — never at module-import time (CLAUDE.md: "No heavy or optional
  dependency is imported at module import time" / "Do not connect during
  module import", task §4A).
- Transport requirements (SSL, channel binding, pooled endpoint) are
  driven by `ap_agent.config.postgres.PostgresTransportPolicy` instead of
  the notebook's hardcoded Neon-only asserts; the notebook's additional
  `current_database() == 'neondb'` assertion is dropped entirely (task
  §4A: "Do not require the database name to be `neondb`").
- The DSN is never printed or included in an exception message
  (`redact_dsn`); every diagnostic below reports only non-secret transport
  metadata, matching the notebook's own "PostgreSQL DSN: REDACTED" /
  "Credentials in output: NONE" behaviour.
- `psycopg`/`psycopg_pool` are imported lazily, inside the functions that
  need them, not at module scope (CLAUDE.md).
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator

from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy
from ap_agent.exceptions import PostgresConfigurationError, TenantContextError

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    import psycopg

__all__ = [
    "memory_utc_now",
    "redact_dsn",
    "dsn_is_configured",
    "load_dsn",
    "inspect_transport",
    "verify_transport_policy",
    "connection_options",
    "open_connection",
    "create_connection_pool",
    "set_tenant_context",
    "TENANT_CONTEXT_SETTING",
]


TENANT_CONTEXT_SETTING = "ap_agent.tenant_id"

# Matches the password component of a `postgresql://user:PASSWORD@host/db`
# DSN so it can be masked out of any diagnostic string. Deliberately
# conservative: only strips between the first `:` after `//user` and the
# next `@`.
_DSN_PASSWORD_PATTERN = re.compile(r"(://[^:/@]+:)([^@/]+)(@)")


def memory_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def redact_dsn(dsn: str) -> str:
    """Return `dsn` with any password component replaced by `***`.

    Used only for diagnostics/exception messages; the real DSN is never
    logged or printed (task §4A: "Never print either DSN").
    """

    if not dsn:
        return dsn

    return _DSN_PASSWORD_PATTERN.sub(r"\1***\3", dsn)


def dsn_is_configured(config: MemoryConfig, *, for_migration: bool = False) -> bool:
    env_var = (
        config.migration_dsn_environment_variable
        if for_migration
        else config.dsn_environment_variable
    )

    return bool(os.environ.get(env_var, "").strip())


def load_dsn(config: MemoryConfig, *, for_migration: bool = False) -> str:
    """Load a PostgreSQL DSN from the environment.

    Mirrors the notebook's `load_postgres_dsn` validation (non-empty,
    `postgresql://`/`postgres://` prefix) without the Colab dependency.
    """

    env_var = (
        config.migration_dsn_environment_variable
        if for_migration
        else config.dsn_environment_variable
    )

    dsn = os.environ.get(env_var, "").strip()

    if not dsn and for_migration and config.allow_migration_dsn_fallback:
        env_var = config.dsn_environment_variable
        dsn = os.environ.get(env_var, "").strip()

    if not dsn:
        raise PostgresConfigurationError(f"{env_var} is not configured.")

    if not dsn.startswith(("postgresql://", "postgres://")):
        raise PostgresConfigurationError(
            f"{env_var} is not a valid PostgreSQL connection string."
        )

    return dsn


def inspect_transport(dsn: str) -> dict[str, Any]:
    """Inspect connection requirements without exposing credentials."""

    from psycopg.conninfo import conninfo_to_dict

    parameters = conninfo_to_dict(dsn)

    ssl_mode = str(parameters.get("sslmode", "")).lower()
    channel_binding = str(parameters.get("channel_binding", "")).lower()
    host = str(parameters.get("host", "")).lower()

    return {
        "ssl_mode": ssl_mode,
        "ssl_enforced": ssl_mode in {"require", "verify-ca", "verify-full"},
        "channel_binding": channel_binding,
        "channel_binding_enforced": channel_binding == "require",
        "pooled_endpoint": "-pooler" in host,
        "database": str(parameters.get("dbname", "")),
    }


def verify_transport_policy(
    dsn: str,
    policy: PostgresTransportPolicy,
) -> dict[str, Any]:
    transport = inspect_transport(dsn)

    if policy.require_ssl and not transport["ssl_enforced"]:
        raise PostgresConfigurationError(
            "The PostgreSQL connection does not require SSL."
        )

    if policy.require_channel_binding and not transport["channel_binding_enforced"]:
        raise PostgresConfigurationError(
            "The PostgreSQL connection does not require channel binding."
        )

    if policy.require_pooled_endpoint and not transport["pooled_endpoint"]:
        raise PostgresConfigurationError(
            "The PostgreSQL connection is not a pooled endpoint."
        )

    return transport


def connection_options(config: MemoryConfig) -> str:
    """`libpq` `options` string applying statement/lock timeouts.

    Mirrors `psycopg.connect(..., prepare_threshold=None)` (task §4A:
    "Use `prepare_threshold=None` where needed for transaction-pooler
    compatibility") applied by the caller alongside this.
    """

    return (
        f"-c statement_timeout={config.statement_timeout_milliseconds} "
        f"-c lock_timeout={config.lock_timeout_milliseconds}"
    )


@contextmanager
def open_connection(dsn: str, config: MemoryConfig) -> Iterator["psycopg.Connection"]:
    """Open one short-lived PostgreSQL connection.

    Verifies `config.transport_policy` before connecting. Never called at
    module-import time; always called explicitly by a repository or
    migration-runner function.
    """

    import psycopg

    verify_transport_policy(dsn, config.transport_policy)

    with psycopg.connect(
        dsn,
        connect_timeout=config.connect_timeout_seconds,
        prepare_threshold=None,
        application_name=config.application_name,
        options=connection_options(config),
    ) as connection:
        yield connection


def create_connection_pool(dsn: str, config: MemoryConfig):
    """Create (but do not open) a `psycopg_pool.ConnectionPool`.

    New in M8 (task §4A: "Add psycopg_pool if client-side pooling is
    implemented"); the notebook opened one ad-hoc connection per call.
    Callers must `.open()`/use as a context manager and `.close()` the
    pool themselves; this function never connects.
    """

    from psycopg_pool import ConnectionPool

    verify_transport_policy(dsn, config.transport_policy)

    return ConnectionPool(
        dsn,
        min_size=config.pool.minimum_size,
        max_size=config.pool.maximum_size,
        max_idle=config.pool.maximum_idle_seconds,
        kwargs={
            "prepare_threshold": None,
            "application_name": config.application_name,
            "options": connection_options(config),
        },
        open=False,
    )


def set_tenant_context(cursor: "psycopg.Cursor", tenant_id: str) -> None:
    """Set `ap_agent.tenant_id` transaction-locally and verify it stuck.

    Source: notebook `set_memory_tenant` (cell 88), verbatim behaviour.
    `TRUE` (the third `set_config` argument) makes the setting
    transaction-local so it cannot leak between requests sharing a pooled
    connection (task §5: "Set tenant context transaction-locally so it
    cannot leak between pooled requests."). Fails closed with
    `TenantContextError` if the read-back does not match, rather than
    silently proceeding with no (or a stale) tenant context.
    """

    cursor.execute(
        "SELECT set_config('ap_agent.tenant_id', %s, TRUE);",
        (str(tenant_id),),
    )

    configured_tenant = cursor.fetchone()[0]

    if configured_tenant != str(tenant_id):
        raise TenantContextError("Tenant context was not applied.")

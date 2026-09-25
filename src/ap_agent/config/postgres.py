"""PostgreSQL configuration for Phase 7 operational memory.

Source: notebook cell 83 ("PHASE 7 — CELL 1")'s `MemoryConfig` values and
`PHASE_7_POSTGRES_DSN_ENV`/`PHASE_7_POSTGRES_SCHEMA` constants, plus cell
84 ("PHASE 7 — CELL 2")'s transport requirements
(`inspect_transport_policy`, the `sslmode`/`channel_binding` asserts in
`verify_postgres_connection`). Ported and generalized per M8 task brief
§3/§4:

- The notebook hardcodes Neon-only transport requirements (SSL *and*
  channel binding always required, database name asserted to be
  `neondb`) and reads its DSN from Colab `userdata`. This module makes
  SSL/channel-binding/pooled-endpoint enforcement explicit, overridable
  configuration (`PostgresTransportPolicy`) instead, with defaults safe
  for disposable local PostgreSQL (task §4: "Do not require
  channel_binding=require for disposable local PostgreSQL"), and reads
  the DSN from the environment at call time (`ap_agent.db.connection`),
  never at import time and never through Colab.
- `MemoryConfig.default_tenant_id` (the notebook's
  `default_tenant_id="prototype-sme-001"`) is **not** carried into this
  module: task §3.3 requires production to have no dependence on a
  prototype tenant constant. Every repository/service operation takes
  `tenant_id` as an explicit argument instead (task §5); the notebook's
  own `PROTOTYPE_TENANT_ID` derivation lives only in the golden
  integration test fixtures under `tests/`.
- Pool sizing (`PostgresPoolConfig`) is new: the notebook opened one
  ad-hoc `psycopg.connect(...)` per call and never pooled client-side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "RUNTIME_DSN_ENVIRONMENT_VARIABLE",
    "MIGRATION_DSN_ENVIRONMENT_VARIABLE",
    "DEFAULT_SCHEMA_NAME",
    "SCHEMA_NAME_PATTERN",
    "PostgresTransportPolicy",
    "PostgresPoolConfig",
    "MemoryConfig",
]


RUNTIME_DSN_ENVIRONMENT_VARIABLE = "AP_AGENT_POSTGRES_DSN"
MIGRATION_DSN_ENVIRONMENT_VARIABLE = "AP_AGENT_POSTGRES_MIGRATION_DSN"

DEFAULT_SCHEMA_NAME = "ap_agent"

SCHEMA_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class PostgresTransportPolicy:
    """Transport requirements enforced before opening a connection.

    Defaults are safe for disposable/local PostgreSQL (plain `sslmode`
    negotiated, no channel-binding requirement, no pooled-endpoint
    requirement). A deployment connecting through Neon can opt into the
    notebook's stricter policy explicitly:
    `PostgresTransportPolicy(require_ssl=True, require_channel_binding=True,
    require_pooled_endpoint=True)`.
    """

    require_ssl: bool = True
    require_channel_binding: bool = False
    require_pooled_endpoint: bool = False


@dataclass(frozen=True)
class PostgresPoolConfig:
    """Client-side pool sizing for `psycopg_pool.ConnectionPool`."""

    minimum_size: int = 1
    maximum_size: int = 5
    maximum_idle_seconds: float = 300.0


@dataclass(frozen=True)
class MemoryConfig:
    """Configuration for the Phase 7 PostgreSQL operational-memory system.

    Every field here must be threaded explicitly by the caller
    (`ap_agent.db`, `ap_agent.repositories`, `ap_agent.services`); this
    module defines no module-level `MemoryConfig` instance
    (CLAUDE.md: "No module under `src` may define a mutable module-level
    config instance").
    """

    dsn_environment_variable: str = RUNTIME_DSN_ENVIRONMENT_VARIABLE
    migration_dsn_environment_variable: str = MIGRATION_DSN_ENVIRONMENT_VARIABLE

    # For local development only: fall back to the runtime DSN when no
    # dedicated migration-owner DSN is configured (task §4A: "the
    # migration DSN may fall back to the runtime DSN only when explicitly
    # permitted by configuration").
    allow_migration_dsn_fallback: bool = False

    schema_name: str = DEFAULT_SCHEMA_NAME
    schema_version: int = 1

    application_name: str = "accounts-payable-agent-phase-7"

    connect_timeout_seconds: int = 30
    statement_timeout_milliseconds: int = 30000
    lock_timeout_milliseconds: int = 10000

    transport_policy: PostgresTransportPolicy = field(
        default_factory=PostgresTransportPolicy
    )
    pool: PostgresPoolConfig = field(default_factory=PostgresPoolConfig)

    append_only_audit: bool = True
    enforce_tenant_isolation: bool = True

    def __post_init__(self) -> None:
        assert self.schema_version > 0
        assert self.connect_timeout_seconds > 0
        assert self.statement_timeout_milliseconds > 0
        assert self.lock_timeout_milliseconds > 0
        assert self.application_name.strip()
        assert self.dsn_environment_variable.strip()
        assert self.migration_dsn_environment_variable.strip()
        assert SCHEMA_NAME_PATTERN.fullmatch(self.schema_name)
        assert self.pool.minimum_size >= 1
        assert self.pool.maximum_size >= self.pool.minimum_size

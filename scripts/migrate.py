#!/usr/bin/env python3
"""Apply Phase 7 operational-memory migrations (task §9: "A migration
command/service").

Reads the migration-owner DSN from `AP_AGENT_POSTGRES_MIGRATION_DSN`
(falling back to `AP_AGENT_POSTGRES_DSN` only if
`--allow-migration-dsn-fallback` is passed, matching
`MemoryConfig.allow_migration_dsn_fallback`). Never prints a DSN; only
migration ids, checksums and statuses.

Usage:
    python scripts/migrate.py [--allow-migration-dsn-fallback] [--require-ssl]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy
from ap_agent.db.connection import load_dsn
from ap_agent.db.migration_runner import apply_all_migrations
from ap_agent.db.roles import grant_schema_access
from ap_agent.exceptions import MigrationIntegrityError, PostgresConfigurationError


def _grant_runtime_access(dsn: str, config: MemoryConfig) -> None:
    """Grant a runtime application role least-privilege access to the
    `ap_agent` schema (see `ap_agent.db.roles.grant_schema_access`).
    Skipped when `AP_AGENT_POSTGRES_RUNTIME_ROLE` is unset (e.g. against
    Neon, where a role is typically provisioned by the platform instead).
    """

    role_name = os.environ.get("AP_AGENT_POSTGRES_RUNTIME_ROLE", "").strip()

    if not role_name:
        print("AP_AGENT_POSTGRES_RUNTIME_ROLE not set; skipping grant step.")
        return

    grant_schema_access(dsn, config, role_name=role_name)

    print(f"Granted runtime access on schema {config.schema_name!r} to role {role_name!r}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-migration-dsn-fallback",
        action="store_true",
        help="Fall back to AP_AGENT_POSTGRES_DSN when no migration-owner DSN is set.",
    )
    parser.add_argument(
        "--require-ssl",
        action="store_true",
        help="Require SSL on the migration connection (off by default for local/disposable PostgreSQL).",
    )
    args = parser.parse_args()

    config = MemoryConfig(
        allow_migration_dsn_fallback=args.allow_migration_dsn_fallback,
        transport_policy=PostgresTransportPolicy(require_ssl=args.require_ssl),
    )

    try:
        dsn = load_dsn(config, for_migration=True)
    except PostgresConfigurationError as exc:
        print(f"Migration DSN error: {exc}", file=sys.stderr)
        return 2

    try:
        results = apply_all_migrations(dsn, config)
    except MigrationIntegrityError as exc:
        print(f"Migration integrity error: {exc}", file=sys.stderr)
        return 3

    for result in results:
        print(f"{result['migration_id']}: {result['status']} (checksum {result['checksum'][:12]}...)")

    _grant_runtime_access(dsn, config)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

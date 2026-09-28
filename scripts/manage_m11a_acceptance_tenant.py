#!/usr/bin/env python3
"""Seed/clean up an isolated acceptance tenant for the M11A real
FastAPI + PostgreSQL + Next.js acceptance run (task §15).

Root cause this script fixes: the first version of
`scripts/run-real-integration.mjs` pointed Next.js at the all-zero
placeholder tenant UUID (`AP_AGENT_DEV_TENANT_ID` default from
`.env.example`) and never seeded any data for it, so the real `ap_agent_m8_test`
database correctly reported an empty dashboard (all zeros) for that
tenant -- CI's failure was the system working as designed against
unseeded input, not a bug in the dashboard/API. Every acceptance run now
gets its **own**, freshly generated tenant UUID with the exact
controlled-fixture baseline seeded into it, so the numbers this script's
caller asserts against are real and reproducible without colliding with
any other tenant, past or future, in the shared test database.

Two subcommands:

    seed --output <path>
        Creates (idempotently) a dedicated least-privilege PostgreSQL
        runtime role (NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE,
        `ap_agent.db.roles.create_least_privilege_role` -- the same
        mechanism M8/M9/M10's own `requires_postgres` suites use, never a
        bespoke one), registers a brand-new tenant, and seeds exactly:
          - 1 automatically-completed invoice (no review required)
          - 3 review-required invoices/cases, whose review reasons are
            distributed so the dashboard's review-reason analytics read
            INHERITED_FINANCIAL_VALIDATION_REVIEW=3, SUPPLIER_NAME_MISSING=2,
            INVOICE_LINE_TOTAL_MISSING=1 -- the exact M11A task-brief
            baseline (task §9). Writes `{"tenant_id": ..., "runtime_dsn": ...}`
            as JSON to `--output` (mode 0600, never printed to stdout);
            only the tenant id (a random UUID, not a secret) is printed.

    cleanup --tenant-id <uuid>
        Deletes every row this run's tenant can still legally have
        deleted: `ap_agent.review_cases`. It does **not** attempt to
        delete `ap_agent.workflow_instances`, `ap_agent.invoice_memory_records`,
        or `ap_agent.tenants` -- this is not a gap, it is the schema's own
        design: `invoice_memory_records` carries a
        `BEFORE UPDATE OR DELETE` trigger that unconditionally rejects
        mutation (`ap_agent.reject_append_only_mutation()`,
        `0003_matched_invoice_memory.sql`) for *any* role including the
        table owner, and `workflow_instances`/`tenants` are each
        referenced by an `ON DELETE RESTRICT` foreign key from a table
        that can never lose its rows -- so they become permanently
        undeletable the moment an invoice-memory row exists for them.
        Every acceptance run therefore leaves a small, permanent,
        harmlessly-inert audit trail behind for its own never-reused
        tenant id, by the same deliberate immutability guarantee M8
        documents for production data; only the removable `review_cases`
        rows are cleaned up, so the shared database doesn't accumulate
        stale "OPEN" work items across runs.

Reads only `AP_AGENT_TEST_POSTGRES_DSN` (never `AP_AGENT_POSTGRES_DSN`/
`AP_AGENT_POSTGRES_MIGRATION_DSN` -- those name production DSNs), verifies
its `dbname` is `ap_agent_m8_test` before opening any connection (the same
fail-closed gate `tests/integration/test_memory_postgres_integration.py`/
`tests/api/conftest.py` use), and never prints, logs, or writes the DSN or
any derived password to stdout/stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

TEST_DSN_ENV_VAR = "AP_AGENT_TEST_POSTGRES_DSN"
EXPECTED_TEST_DATABASE = "ap_agent_m8_test"

# Distinct from M8's `ap_agent_m8_test_runtime` and M10's
# `ap_agent_m10_test_runtime` (tests/api/conftest.py) so this script's own
# password rotation (`create_least_privilege_role` rotates the password of
# an already-least-privilege existing role) never races a concurrently
# running M8/M9/M10 workflow's use of its own role.
RUNTIME_ROLE_NAME = "ap_agent_m11a_acceptance_runtime"

INHERITED_FINANCIAL_VALIDATION_REVIEW = "INHERITED_FINANCIAL_VALIDATION_REVIEW"
SUPPLIER_NAME_MISSING = "SUPPLIER_NAME_MISSING"
INVOICE_LINE_TOTAL_MISSING = "INVOICE_LINE_TOTAL_MISSING"


def _owner_dsn_or_fail() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR, "").strip()

    if not dsn:
        print(
            f"{TEST_DSN_ENV_VAR} is not set. This script seeds/cleans up the real "
            "FastAPI+PostgreSQL acceptance tenant (M11A task §15) and needs a live "
            "database; it never substitutes a mock result.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    from psycopg.conninfo import conninfo_to_dict

    dbname = conninfo_to_dict(dsn).get("dbname")

    if dbname != EXPECTED_TEST_DATABASE:
        print(
            f"{TEST_DSN_ENV_VAR} targets database {dbname!r}, expected "
            f"{EXPECTED_TEST_DATABASE!r}. Refusing to run (fail-closed): this script "
            "creates roles and seeds/deletes rows and must never run against a "
            "database that might be production.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return dsn


def _local_config():
    from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy

    return MemoryConfig(
        transport_policy=PostgresTransportPolicy(require_ssl=True, require_channel_binding=True),
    )


def _ensure_runtime_dsn(owner_dsn: str, config) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from ap_agent.db import roles as db_roles
    from ap_agent.db.migration_runner import apply_all_migrations

    # Idempotent: a no-op if the M8/M9/M10 workflow (or an earlier M11A
    # run) already applied these migrations to this database.
    apply_all_migrations(owner_dsn, config)

    password = secrets.token_urlsafe(32)
    db_roles.create_least_privilege_role(owner_dsn, config, role_name=RUNTIME_ROLE_NAME, password=password)
    db_roles.grant_schema_access(owner_dsn, config, role_name=RUNTIME_ROLE_NAME)

    flags = db_roles.role_privilege_flags(owner_dsn, config, role_name=RUNTIME_ROLE_NAME)
    if flags["superuser"] or flags["bypassrls"]:
        print(
            f"Refusing to use {RUNTIME_ROLE_NAME!r} as the FastAPI runtime role: it "
            f"unexpectedly carries an elevated privilege flag ({flags}).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    params = conninfo_to_dict(owner_dsn)
    params["user"] = RUNTIME_ROLE_NAME
    params["password"] = password
    return make_conninfo(**params)


def cmd_seed(args: argparse.Namespace) -> int:
    owner_dsn = _owner_dsn_or_fail()
    config = _local_config()

    runtime_dsn = _ensure_runtime_dsn(owner_dsn, config)

    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository
    from tests.support.review_fixtures import seed_completed_invoice, seed_review_case

    tenant_id = uuid.uuid4()

    PostgresMemoryRepository(runtime_dsn, config).register_tenant(
        tenant_id=tenant_id,
        tenant_key=f"m11a-acceptance-{tenant_id.hex[:8]}",
        display_name="M11A Acceptance Test Tenant",
    )

    # 1 automatically-completed invoice: dashboard's "Completed automatically" = 1.
    seed_completed_invoice(runtime_dsn, config, tenant_id=tenant_id)

    # 3 review-required invoices, reasons distributed for the exact
    # task-brief baseline: INHERITED_FINANCIAL_VALIDATION_REVIEW=3 (present
    # on all three, as its name implies), SUPPLIER_NAME_MISSING=2,
    # INVOICE_LINE_TOTAL_MISSING=1.
    seed_review_case(
        runtime_dsn, config, tenant_id=tenant_id,
        review_reasons=(INHERITED_FINANCIAL_VALIDATION_REVIEW, SUPPLIER_NAME_MISSING),
    )
    seed_review_case(
        runtime_dsn, config, tenant_id=tenant_id,
        review_reasons=(INHERITED_FINANCIAL_VALIDATION_REVIEW, SUPPLIER_NAME_MISSING),
    )
    seed_review_case(
        runtime_dsn, config, tenant_id=tenant_id,
        review_reasons=(INHERITED_FINANCIAL_VALIDATION_REVIEW, INVOICE_LINE_TOTAL_MISSING),
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Create with 0600 from the start (not chmod after) so the DSN/password
    # is never briefly world/group-readable on disk.
    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as handle:
        json.dump({"tenant_id": str(tenant_id), "runtime_dsn": runtime_dsn}, handle)

    print(f"Seeded M11A acceptance tenant {tenant_id} (4-invoice controlled-fixture baseline).")
    print(f"Runtime DSN written to {output_path} (0600, never printed).")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    owner_dsn = _owner_dsn_or_fail()
    config = _local_config()

    tenant_id = uuid.UUID(args.tenant_id)

    from ap_agent.db.connection import open_connection, set_tenant_context

    with open_connection(owner_dsn, config) as connection:
        with connection.cursor() as cursor:
            set_tenant_context(cursor, str(tenant_id))
            cursor.execute("DELETE FROM ap_agent.review_cases WHERE tenant_id = %s;", (tenant_id,))
            deleted_review_cases = cursor.rowcount

    print(
        f"Cleaned up M11A acceptance tenant {tenant_id}: deleted {deleted_review_cases} "
        "review_cases row(s)."
    )
    print(
        "workflow_instances/invoice_memory_records/tenants rows for this tenant are left "
        "in place -- ap_agent.invoice_memory_records is append-only by an unconditional "
        "database trigger, and workflow_instances/tenants are each referenced by an "
        "ON DELETE RESTRICT foreign key from it, so they are permanently undeletable "
        "once seeded, by the schema's own design (not a bug in this script)."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed_parser = subparsers.add_parser("seed", help="Seed a fresh isolated acceptance tenant.")
    seed_parser.add_argument("--output", required=True, help="Path to write {tenant_id, runtime_dsn} JSON to (0600).")
    seed_parser.set_defaults(func=cmd_seed)

    cleanup_parser = subparsers.add_parser("cleanup", help="Delete a tenant's removable rows.")
    cleanup_parser.add_argument("--tenant-id", required=True, help="The tenant id `seed` generated.")
    cleanup_parser.set_defaults(func=cmd_cleanup)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Seed / verify / clean up isolated tenants for M11D Core (operations console).

Reuses the M11A acceptance-tenant machinery
(`scripts/manage_m11a_acceptance_tenant.py`: fail-closed DSN gate, migration
application, least-privilege runtime role) under its own role name so a
concurrent M8-M11C run's password rotation is never disturbed.

Subcommands
-----------
seed --output <path> [--other-tenant]
    Applies the migrations, creates a fresh least-privilege runtime role and a
    fresh tenant (plus, with `--other-tenant`, a second one for cross-tenant
    checks). Writes `{tenant_id, other_tenant_id, runtime_dsn}` as JSON to
    `--output` (0600, never printed). Only tenant ids are printed.

verify --state <path> [--expect-resume]
    Reads the seed output and inspects PostgreSQL *directly* (as the
    least-privilege runtime role, RLS on) to prove what the browser run
    actually did: every job reached a terminal state, the upload job's
    persisted workflow and original memory verify and are unchanged, the
    resume job (when expected) executed only from its restart stage and
    appended exactly one derived version, append-only tables refuse
    UPDATE/DELETE, and the other tenant sees nothing. Exits non-zero on any
    failed expectation.

cleanup --tenant-id <uuid> [--tenant-id <uuid> ...]
    Deletes the removable `review_cases` rows (those no decision, job or
    derived version references). Jobs, job events, decisions, audit events,
    invoice memory, derived versions, workflows and tenants are append-only or
    restricted by schema design and stay behind, exactly like M11A-M11C.

Reads only `AP_AGENT_TEST_POSTGRES_DSN`, fails closed unless its database is
exactly `ap_agent_m8_test`, and never prints a DSN or password.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

import manage_m11a_acceptance_tenant as base  # noqa: E402

base.RUNTIME_ROLE_NAME = "ap_agent_m11d_runtime"


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _register_tenant(runtime_dsn: str, config: Any, label: str) -> uuid.UUID:
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    tenant_id = uuid.uuid4()
    PostgresMemoryRepository(runtime_dsn, config).register_tenant(
        tenant_id=tenant_id, tenant_key=f"m11d-{label}-{tenant_id.hex[:10]}", display_name=f"M11D {label} tenant"
    )
    return tenant_id


def cmd_seed(args: argparse.Namespace) -> int:
    owner_dsn = base._owner_dsn_or_fail()
    config = base._local_config()
    runtime_dsn = base._ensure_runtime_dsn(owner_dsn, config)

    tenant_id = _register_tenant(runtime_dsn, config, "ops")
    state: dict[str, Any] = {"tenant_id": str(tenant_id), "runtime_dsn": runtime_dsn, "other_tenant_id": None}

    if args.other_tenant:
        state["other_tenant_id"] = str(_register_tenant(runtime_dsn, config, "other"))

    _write_private_json(Path(args.output), state)
    print(f"Seeded M11D tenant {tenant_id}" + (f" and other tenant {state['other_tenant_id']}." if args.other_tenant else "."))
    return 0


class _Checks:
    def __init__(self) -> None:
        self.failures = 0
        self.total = 0

    def check(self, name: str, condition: bool, detail: Any = "") -> None:
        self.total += 1
        if not condition:
            self.failures += 1
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition or detail == "" else f" -- {detail}"))


def cmd_verify(args: argparse.Namespace) -> int:
    import psycopg

    from ap_agent.db.connection import open_connection, set_tenant_context
    from ap_agent.serialization.memory_json import canonical_payload_sha256

    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    config = base._local_config()
    tenant_id = uuid.UUID(state["tenant_id"])
    dsn = state["runtime_dsn"]
    checks = _Checks()

    def query(sql: str, params: tuple = (), tenant: uuid.UUID | None = tenant_id) -> list[tuple]:
        with open_connection(dsn, config) as connection:
            with connection.cursor() as cursor:
                if tenant is not None:
                    set_tenant_context(cursor, str(tenant))
                cursor.execute(sql, params)
                return cursor.fetchall()

    print("M11D real-stack database verification")

    jobs = query(
        "SELECT job_id, job_type, status, error_code, result_summary, workflow_id, result_review_id, result_version_id "
        "FROM ap_agent.workflow_jobs WHERE tenant_id = %s ORDER BY created_at;",
        (tenant_id,),
    )
    checks.check("at least one job exists for the tenant", len(jobs) >= 1, len(jobs))
    checks.check("no job is left QUEUED or RUNNING", all(job[2] not in {"QUEUED", "RUNNING"} for job in jobs), [job[2] for job in jobs])
    checks.check("no job failed", all(job[2] != "FAILED" for job in jobs), [(job[1], job[3]) for job in jobs if job[2] == "FAILED"])

    uploads = [job for job in jobs if job[1] == "PROCESS_DOCUMENT"]
    resumes = [job for job in jobs if job[1] == "RESUME_WORKFLOW"]
    checks.check("exactly one upload job", len(uploads) == 1, len(uploads))

    if uploads:
        upload = uploads[0]
        workflow_id = upload[5]
        (memory,) = query(
            "SELECT payload_sha256, normalized_invoice, financial_validation, matching_result, matched_reference_data "
            "FROM ap_agent.invoice_memory_records WHERE tenant_id = %s AND workflow_id = %s;",
            (tenant_id, workflow_id),
        )
        recomputed = canonical_payload_sha256(
            {"normalization_result": memory[1], "financial_validation_result": memory[2],
             "matching_result": memory[3], "matched_reference_data": memory[4]}
        )
        checks.check("original invoice memory verifies against its stored hash (unchanged)", recomputed == memory[0].strip())

        events = query(
            "SELECT event_type, stage FROM ap_agent.workflow_job_events WHERE tenant_id = %s AND job_id = %s ORDER BY sequence_number;",
            (tenant_id, upload[0]),
        )
        started = [stage for event_type, stage in events if event_type == "STAGE_STARTED"]
        checks.check(
            "upload job ran the seven pipeline stages in order",
            started == ["INGESTION", "PREPROCESSING", "OCR", "NORMALIZATION", "FINANCIAL_VALIDATION", "REFERENCE_MATCHING", "MEMORY_PERSISTENCE"],
            started,
        )

        if args.expect_resume:
            checks.check("exactly one resume job", len(resumes) == 1, len(resumes))

            if resumes:
                resume = resumes[0]
                summary = resume[4]
                resume_events = query(
                    "SELECT event_type, stage FROM ap_agent.workflow_job_events WHERE tenant_id = %s AND job_id = %s ORDER BY sequence_number;",
                    (tenant_id, resume[0]),
                )
                resumed_started = [stage for event_type, stage in resume_events if event_type == "STAGE_STARTED"]
                checks.check("resume job executed only from MEMORY_PERSISTENCE", resumed_started == ["MEMORY_PERSISTENCE"], resumed_started)
                checks.check("resume summary records the restart stage", summary.get("restart_stage") == "MEMORY_PERSISTENCE", summary.get("restart_stage"))
                checks.check("no ingestion/preprocessing/OCR/normalization during the resume",
                             not {"INGESTION", "PREPROCESSING", "OCR", "NORMALIZATION"} & set(resumed_started))

                versions = query(
                    "SELECT version_id, payload_sha256, review_required, terminal_status FROM ap_agent.invoice_memory_versions "
                    "WHERE tenant_id = %s AND workflow_id = %s;",
                    (tenant_id, workflow_id),
                )
                checks.check("exactly one derived version appended", len(versions) == 1, len(versions))
                checks.check("the derived version is the job's result", bool(versions) and resume[7] == versions[0][0])

                (workflow,) = query(
                    "SELECT current_phase, current_status, review_required FROM ap_agent.workflow_instances WHERE tenant_id = %s AND workflow_id = %s;",
                    (tenant_id, workflow_id),
                )
                settled = ("COMPLETED", "SUCCEEDED", False) if resume[2] == "SUCCEEDED" else ("HUMAN_REVIEW", "REVIEW_REQUIRED", True)
                checks.check("workflow is settled consistently with the resume outcome", tuple(workflow) == settled, workflow)

                audit = [row[0] for row in query(
                    "SELECT event_type FROM ap_agent.audit_events WHERE tenant_id = %s AND workflow_id = %s;", (tenant_id, workflow_id))]
                checks.check("one resume request and one resume execution are audited",
                             audit.count("WORKFLOW_RESUME_REQUESTED") == 1 and audit.count("WORKFLOW_RESUME_EXECUTED") == 1, audit)
        else:
            checks.check("no resume job when none was requested", len(resumes) == 0, len(resumes))

    def refused(sql: str) -> bool:
        try:
            with open_connection(dsn, config) as connection:
                with connection.cursor() as cursor:
                    set_tenant_context(cursor, str(tenant_id))
                    cursor.execute(sql, (tenant_id,))
            return False
        except psycopg.errors.Error:
            return True

    checks.check("job events refuse UPDATE", refused("UPDATE ap_agent.workflow_job_events SET message = 'x' WHERE tenant_id = %s;"))
    checks.check("job events refuse DELETE", refused("DELETE FROM ap_agent.workflow_job_events WHERE tenant_id = %s;"))
    checks.check("jobs refuse DELETE", refused("DELETE FROM ap_agent.workflow_jobs WHERE tenant_id = %s;"))
    checks.check("derived versions refuse UPDATE", refused("UPDATE ap_agent.invoice_memory_versions SET review_required = NOT review_required WHERE tenant_id = %s;"))
    checks.check("original memory refuses UPDATE", refused("UPDATE ap_agent.invoice_memory_records SET review_required = NOT review_required WHERE tenant_id = %s;"))

    if state.get("other_tenant_id"):
        other = uuid.UUID(state["other_tenant_id"])
        foreign = query("SELECT COUNT(*) FROM ap_agent.workflow_jobs;", (), tenant=other)[0][0]
        checks.check("another tenant sees none of these jobs (row-level security)", foreign == 0, foreign)

    unscoped = query("SELECT COUNT(*) FROM ap_agent.workflow_jobs;", (), tenant=None)[0][0]
    checks.check("without tenant or worker scope no job is visible", unscoped == 0, unscoped)

    print(f"{checks.total - checks.failures}/{checks.total} database checks passed.")
    return 1 if checks.failures else 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    owner_dsn = base._owner_dsn_or_fail()
    config = base._local_config()

    from ap_agent.db.connection import open_connection, set_tenant_context

    for raw in args.tenant_id:
        tenant_id = uuid.UUID(raw)

        with open_connection(owner_dsn, config) as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))
                cursor.execute(
                    """
                    DELETE FROM ap_agent.review_cases AS review
                    WHERE review.tenant_id = %s
                      AND NOT EXISTS (SELECT 1 FROM ap_agent.review_decisions d
                                      WHERE d.tenant_id = review.tenant_id AND d.review_id = review.review_id)
                      AND NOT EXISTS (SELECT 1 FROM ap_agent.workflow_jobs j
                                      WHERE j.tenant_id = review.tenant_id
                                        AND (j.review_id = review.review_id OR j.result_review_id = review.review_id))
                      AND NOT EXISTS (SELECT 1 FROM ap_agent.invoice_memory_versions v
                                      WHERE v.tenant_id = review.tenant_id AND v.new_review_id = review.review_id);
                    """,
                    (tenant_id,),
                )
                deleted = cursor.rowcount

        print(f"Cleaned up M11D tenant {tenant_id}: deleted {deleted} review_cases row(s).")

    print(
        "Remaining by schema design: workflow_jobs and workflow_job_events (append-only / guarded), review_decisions, "
        "audit_events, invoice_memory_records, invoice_memory_versions, workflow_instances and tenants. "
        "They are isolated by row-level security and inert."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed = subparsers.add_parser("seed", help="Seed a fresh isolated tenant.")
    seed.add_argument("--output", required=True)
    seed.add_argument("--other-tenant", action="store_true")

    verify = subparsers.add_parser("verify", help="Inspect PostgreSQL directly.")
    verify.add_argument("--state", required=True)
    verify.add_argument("--expect-resume", action="store_true")

    cleanup = subparsers.add_parser("cleanup", help="Delete removable rows.")
    cleanup.add_argument("--tenant-id", action="append", required=True)

    arguments = parser.parse_args()
    return {"seed": cmd_seed, "verify": cmd_verify, "cleanup": cmd_cleanup}[arguments.command](arguments)


if __name__ == "__main__":
    sys.exit(main())

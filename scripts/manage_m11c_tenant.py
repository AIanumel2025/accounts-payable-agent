#!/usr/bin/env python3
"""Seed / verify / clean up isolated tenants for M11C (interactive review actions).

Reuses the M11A/M11B acceptance-tenant machinery
(`scripts/manage_m11a_acceptance_tenant.py`: DSN gate, least-privilege
runtime role, `.cleanup` semantics) but with its own runtime role so a
concurrent M8-M11B run's password rotation is never disturbed.

Subcommands
-----------
seed --profile acceptance|demo --output <path>
    Creates a fresh tenant (and, for `acceptance`, a second tenant for the
    cross-tenant checks) and seeds distinct review cases -- one per scenario,
    so one scenario's state transition never invalidates another's
    (M11C task §24). Writes `{tenant ids, runtime_dsn, case ids, original-
    memory digests}` as JSON to `--output` (0600, never printed). Only safe
    values (tenant id, case keys/ids) are printed.

verify --state <path>
    Reads the seed output and inspects PostgreSQL *directly* (as the
    least-privilege runtime role, RLS on) to prove what the browser scenarios
    actually did: append-only decisions, audit events, workflow/case
    revisions, original-memory immutability, resume handoff payloads, tenant
    isolation and that append-only tables refuse UPDATE/DELETE. Exits non-zero
    if any expectation fails.

cleanup --tenant-id <uuid> [--tenant-id <uuid> ...]
    Deletes the removable `review_cases` rows. `invoice_memory_records`,
    `review_decisions` and `audit_events` are append-only by trigger, so the
    tenants, workflows and those rows stay behind by schema design (same
    documented residue as M11A/M11B).

Reads only `AP_AGENT_TEST_POSTGRES_DSN`, fails closed unless its database is
exactly `ap_agent_m8_test`, and never prints a DSN or password.
"""

from __future__ import annotations

import argparse
import hashlib
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

base.RUNTIME_ROLE_NAME = "ap_agent_m11c_runtime"

ACCEPTANCE_CASE_KEYS = (
    "claim_release", "approve_resume", "correct_line", "correct_header", "reject", "concurrent", "idempotency",
    "stale", "authz", "invalid_correction", "payment", "validation_only", "spare",
)
OTHER_TENANT_CASE_KEYS = ("cross_tenant",)
DEMO_NAMED_KEYS = ("template1", "08181_warped", "aaron_bergman")
DEMO_GENERIC = (
    ("demo_a", "Demo invoice A (purchase order 99).pdf"),
    ("demo_b", "Demo invoice B (purchase order 99).pdf"),
    ("demo_c", "Demo invoice C (purchase order 99).pdf"),
    ("demo_d", "Demo invoice D (purchase order 99).pdf"),
)

REVIEWER_A = "real-reviewer-a"
REVIEWER_B = "real-reviewer-b"


# ------------------------------------------------------------
# Direct, tenant-scoped PostgreSQL inspection (runtime role, RLS on)
# ------------------------------------------------------------


class Inspector:
    def __init__(self, runtime_dsn: str, tenant_id: uuid.UUID) -> None:
        self.dsn = runtime_dsn
        self.tenant_id = tenant_id
        self.config = base._local_config()

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        from ap_agent.db.connection import open_connection, set_tenant_context

        with open_connection(self.dsn, self.config) as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(self.tenant_id))
                cursor.execute(sql, params)
                return cursor.fetchall()

    def case(self, case_id: str) -> dict[str, Any]:
        row = self.query(
            "SELECT review.review_status, review.assigned_to, review.resolution_code, review.workflow_id, "
            "workflow.lock_version, workflow.current_phase, workflow.current_status, workflow.review_required "
            "FROM ap_agent.review_cases AS review JOIN ap_agent.workflow_instances AS workflow "
            "ON workflow.tenant_id = review.tenant_id AND workflow.workflow_id = review.workflow_id "
            "WHERE review.tenant_id = %s AND review.review_id = %s;",
            (self.tenant_id, uuid.UUID(case_id)),
        )[0]
        return {
            "status": row[0], "assigned_to": row[1], "resolution": row[2], "workflow_id": row[3],
            "lock_version": int(row[4]), "phase": row[5], "workflow_status": row[6], "review_required": row[7],
        }

    def decisions(self, case_id: str) -> list[dict[str, Any]]:
        return [
            {"id": r[0], "type": r[1], "by": r[2], "notes": r[3], "evidence": r[4]}
            for r in self.query(
                "SELECT decision_id, decision_type, decided_by, decision_notes, evidence "
                "FROM ap_agent.review_decisions WHERE tenant_id = %s AND review_id = %s "
                "ORDER BY decided_at, decision_id;",
                (self.tenant_id, uuid.UUID(case_id)),
            )
        ]

    def events(self, workflow_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            {"type": r[0], "actor": r[1], "payload": r[2]}
            for r in self.query(
                "SELECT event_type, actor_id, payload FROM ap_agent.audit_events "
                "WHERE tenant_id = %s AND workflow_id = %s AND event_type IN "
                "('INTERFACE_COMMAND_EXECUTED', 'WORKFLOW_RESUME_REQUESTED') ORDER BY sequence_number;",
                (self.tenant_id, workflow_id),
            )
        ]

    def memory_digest(self, workflow_id: uuid.UUID) -> dict[str, str]:
        row = self.query(
            "SELECT payload_sha256, normalized_invoice FROM ap_agent.invoice_memory_records "
            "WHERE tenant_id = %s AND workflow_id = %s;",
            (self.tenant_id, workflow_id),
        )[0]
        return {
            "payload_sha256": str(row[0]).strip(),
            "normalized_sha256": hashlib.sha256(json.dumps(row[1], sort_keys=True).encode()).hexdigest(),
        }


# ------------------------------------------------------------
# seed
# ------------------------------------------------------------


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)


def _register_tenant(runtime_dsn: str, config: Any, label: str) -> uuid.UUID:
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    tenant_id = uuid.uuid4()
    PostgresMemoryRepository(runtime_dsn, config).register_tenant(
        tenant_id=tenant_id, tenant_key=f"m11c-{label}-{tenant_id.hex[:8]}", display_name=f"M11C {label} tenant"
    )
    return tenant_id


def cmd_seed(args: argparse.Namespace) -> int:
    from tests.support.review_fixtures import seed_named_review_case, seed_review_case

    owner_dsn = base._owner_dsn_or_fail()
    config = base._local_config()
    runtime_dsn = base._ensure_runtime_dsn(owner_dsn, config)

    tenant_id = _register_tenant(runtime_dsn, config, args.profile)
    state: dict[str, Any] = {"profile": args.profile, "tenant_id": str(tenant_id), "runtime_dsn": runtime_dsn, "cases": {}}
    inspector = Inspector(runtime_dsn, tenant_id)

    def record(target: dict[str, Any], key: str, seeded: Any, inspect: Inspector) -> None:
        target[key] = {
            "review_case_id": str(seeded.review_case_id),
            "workflow_id": str(seeded.workflow_id),
            "memory": inspect.memory_digest(seeded.workflow_id),
        }

    if args.profile == "acceptance":
        for key in ACCEPTANCE_CASE_KEYS:
            seeded = seed_review_case(
                runtime_dsn, config, tenant_id=tenant_id, source_name=f"m11c-{key.replace('_', '-')}.pdf",
            )
            record(state["cases"], key, seeded, inspector)

        other_tenant_id = _register_tenant(runtime_dsn, config, "other")
        other = Inspector(runtime_dsn, other_tenant_id)
        state["other_tenant_id"] = str(other_tenant_id)
        state["other_cases"] = {}
        for key in OTHER_TENANT_CASE_KEYS:
            seeded = seed_review_case(runtime_dsn, config, tenant_id=other_tenant_id, source_name="m11c-other-tenant.pdf")
            record(state["other_cases"], key, seeded, other)
    else:
        # Demo: the three real named fixtures (real fields/evidence) plus a few generic cases with line items.
        for key in DEMO_NAMED_KEYS:
            seeded = seed_named_review_case(runtime_dsn, config, tenant_id=tenant_id, fixture_key=key)
            record(state["cases"], key, seeded, inspector)
        for key, source_name in DEMO_GENERIC:
            seeded = seed_review_case(runtime_dsn, config, tenant_id=tenant_id, source_name=source_name)
            record(state["cases"], key, seeded, inspector)

    _write_private_json(Path(args.output), state)
    print(f"Seeded M11C {args.profile} tenant {tenant_id} with {len(state['cases'])} review case(s).")
    if "other_tenant_id" in state:
        print(f"Seeded second (cross-tenant) tenant {state['other_tenant_id']}.")
    print("State file written (0600; it holds a database credential and is never printed).")
    return 0


# ------------------------------------------------------------
# verify
# ------------------------------------------------------------


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, name: str, condition: bool, detail: Any = "") -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failures.append(name)
            print(f"  FAIL  {name}  {detail}")


def _restart_stage(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event["type"] == "WORKFLOW_RESUME_REQUESTED":
            return event["payload"].get("restart_stage")
    return None


def cmd_verify(args: argparse.Namespace) -> int:
    state = json.loads(Path(args.state).read_text())
    runtime_dsn = state["runtime_dsn"]
    tenant_id = uuid.UUID(state["tenant_id"])
    db = Inspector(runtime_dsn, tenant_id)
    checks = Checks()

    def facts(key: str) -> dict[str, Any]:
        info = state["cases"][key]
        case = db.case(info["review_case_id"])
        events = db.events(case["workflow_id"])
        return {
            "case": case, "events": events, "decisions": db.decisions(info["review_case_id"]),
            "memory": db.memory_digest(case["workflow_id"]), "info": info,
        }

    def executed(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [event for event in events if event["type"] == "INTERFACE_COMMAND_EXECUTED"]

    def memory_unchanged(name: str, f: dict[str, Any]) -> None:
        checks.check(f"{name}: original invoice memory hash unchanged", f["memory"]["payload_sha256"] == f["info"]["memory"]["payload_sha256"])
        checks.check(f"{name}: original normalized invoice unchanged", f["memory"]["normalized_sha256"] == f["info"]["memory"]["normalized_sha256"])

    print("A. claim and release")
    f = facts("claim_release")
    checks.check("returned to OPEN/unassigned", f["case"]["status"] == "OPEN" and f["case"]["assigned_to"] is None, f["case"])
    checks.check("two audit events appended (claim, release)", len(executed(f["events"])) == 2, len(f["events"]))
    checks.check("workflow revision advanced twice", f["case"]["lock_version"] == 3, f["case"]["lock_version"])
    checks.check("no decision recorded", f["decisions"] == [])

    print("B. approve and resume")
    f = facts("approve_resume")
    checks.check("case RESOLVED", f["case"]["status"] == "RESOLVED" and f["case"]["resolution"] == "APPROVED", f["case"])
    checks.check("one append-only APPROVED decision by the reviewer", [(d["type"], d["by"]) for d in f["decisions"]] == [("APPROVED", REVIEWER_A)], f["decisions"])
    checks.check("claim + decision + resume audit events", [e["type"] for e in f["events"]] == ["INTERFACE_COMMAND_EXECUTED", "INTERFACE_COMMAND_EXECUTED", "WORKFLOW_RESUME_REQUESTED"], [e["type"] for e in f["events"]])
    checks.check("restart stage MEMORY_PERSISTENCE (approved, no corrections)", _restart_stage(f["events"]) == "MEMORY_PERSISTENCE", _restart_stage(f["events"]))
    checks.check("workflow revision advanced three times", f["case"]["lock_version"] == 4, f["case"]["lock_version"])
    checks.check("workflow moved to the restart stage (handoff only)", f["case"]["phase"] == "MEMORY_PERSISTENCE" and f["case"]["workflow_status"] == "IN_PROGRESS", f["case"])
    memory_unchanged("approve_resume", f)

    for key, field, stage, line in (("correct_line", "LINE_QUANTITY", "FINANCIAL_VALIDATION", 1), ("correct_header", "PURCHASE_ORDER_NUMBER", "REFERENCE_MATCHING", None)):
        print(f"C. correct and resume ({key})")
        f = facts(key)
        checks.check("case RESOLVED with CORRECTED resolution", f["case"]["status"] == "RESOLVED" and f["case"]["resolution"] == "CORRECTED", f["case"])
        checks.check("one append-only CORRECTED decision", [d["type"] for d in f["decisions"]] == ["CORRECTED"], f["decisions"])
        corrections = f["decisions"][0]["evidence"]["corrections"] if f["decisions"] else []
        checks.check("correction stored with reason and real evidence", len(corrections) == 1 and corrections[0]["field_name"] == field and corrections[0]["line_number"] == line and corrections[0]["evidence_reference_ids"] == ["ev-po-1"] and len(corrections[0]["reason"]) >= 5, corrections)
        checks.check(f"restart stage {stage}", _restart_stage(f["events"]) == stage, _restart_stage(f["events"]))
        memory_unchanged(key, f)

    print("D. reject")
    f = facts("reject")
    checks.check("case closed as REJECTED", f["case"]["status"] == "RESOLVED" and f["case"]["resolution"] == "REJECTED", f["case"])
    checks.check("one append-only REJECTED decision with notes", [d["type"] for d in f["decisions"]] == ["REJECTED"] and bool(f["decisions"][0]["notes"]), f["decisions"])
    checks.check("no resume handoff exists", _restart_stage(f["events"]) is None)
    memory_unchanged("reject", f)

    print("E. concurrent claim")
    f = facts("concurrent")
    checks.check("exactly one owner", f["case"]["status"] == "CLAIMED" and f["case"]["assigned_to"] in {REVIEWER_A, REVIEWER_B}, f["case"])
    checks.check("exactly one claim event", len(executed(f["events"])) == 1, len(f["events"]))
    checks.check("workflow revision advanced exactly once", f["case"]["lock_version"] == 2, f["case"]["lock_version"])
    checks.check("no decision", f["decisions"] == [])

    print("F. idempotency")
    f = facts("idempotency")
    checks.check("claimed once despite a lost response, a retry and a conflicting reuse", f["case"]["status"] == "CLAIMED" and f["case"]["assigned_to"] == REVIEWER_A, f["case"])
    checks.check("no duplicate audit event", len(executed(f["events"])) == 1, len(f["events"]))
    checks.check("workflow revision advanced exactly once", f["case"]["lock_version"] == 2, f["case"]["lock_version"])

    print("G. stale revision")
    f = facts("stale")
    checks.check("the winner's claim stands, the stale attempt changed nothing", f["case"]["assigned_to"] == REVIEWER_B and len(executed(f["events"])) == 1 and f["case"]["lock_version"] == 2, (f["case"], len(f["events"])))

    print("H. authorization")
    f = facts("authz")
    checks.check("still owned by the original reviewer", f["case"]["status"] == "CLAIMED" and f["case"]["assigned_to"] == REVIEWER_A, f["case"])
    checks.check("auditor / other reviewer / cross-tenant attempts left no trace", len(executed(f["events"])) == 1 and f["decisions"] == [] and f["case"]["lock_version"] == 2, (len(f["events"]), f["decisions"]))

    print("I. invalid corrections")
    f = facts("invalid_correction")
    checks.check("no partial mutation: claimed, no decision", f["case"]["status"] == "CLAIMED" and f["decisions"] == [] and len(executed(f["events"])) == 1 and f["case"]["lock_version"] == 2, (f["case"], f["decisions"]))

    print("J. payment prohibition and validation-only")
    for key in ("payment", "validation_only"):
        f = facts(key)
        checks.check(f"{key}: untouched", f["case"]["status"] == "OPEN" and f["case"]["assigned_to"] is None and f["events"] == [] and f["decisions"] == [] and f["case"]["lock_version"] == 1, f["case"])
        memory_unchanged(key, f)

    print("Performance-measurement case (claimed and released by the timing test)")
    f = facts("spare")
    checks.check("spare: claimed then released, nothing else", f["case"]["status"] == "OPEN" and f["case"]["assigned_to"] is None and len(executed(f["events"])) == 2 and f["decisions"] == [], f["case"])
    memory_unchanged("spare", f)

    print("Tenant isolation")
    other = Inspector(runtime_dsn, uuid.UUID(state["other_tenant_id"]))
    for key, info in state["other_cases"].items():
        case = other.case(info["review_case_id"])
        checks.check(f"{key}: other tenant's case untouched", case["status"] == "OPEN" and case["lock_version"] == 1 and other.decisions(info["review_case_id"]) == [] and other.events(case["workflow_id"]) == [], case)
        try:
            db.case(info["review_case_id"])
            visible = True
        except IndexError:
            visible = False
        checks.check(f"{key}: invisible to the primary tenant (row-level security)", not visible)
    total_decisions = int(db.query("SELECT COUNT(*) FROM ap_agent.review_decisions WHERE tenant_id = %s;", (tenant_id,))[0][0])
    checks.check("exactly the expected decisions exist (approve, two corrections, reject)", total_decisions == 4, total_decisions)

    print("Append-only tables")
    for label, sql in (
        ("UPDATE review_decisions", "UPDATE ap_agent.review_decisions SET decision_notes = 'tampered' WHERE tenant_id = %s;"),
        ("DELETE review_decisions", "DELETE FROM ap_agent.review_decisions WHERE tenant_id = %s;"),
        ("UPDATE audit_events", "UPDATE ap_agent.audit_events SET message = 'tampered' WHERE tenant_id = %s;"),
        ("DELETE audit_events", "DELETE FROM ap_agent.audit_events WHERE tenant_id = %s;"),
        ("UPDATE invoice_memory_records", "UPDATE ap_agent.invoice_memory_records SET invoice_number = 'tampered' WHERE tenant_id = %s;"),
    ):
        try:
            db.query(sql, (tenant_id,))
            rejected = False
        except Exception:  # noqa: BLE001 - the append-only trigger raises a database error
            rejected = True
        checks.check(f"{label} is refused by the database", rejected)

    print()
    print(f"{checks.passed} checks passed, {len(checks.failures)} failed.")
    if checks.failures:
        for name in checks.failures:
            print(f"  FAILED: {name}")
        return 1
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Delete the removable rows of each tenant.

    A review case that has a decision can never be deleted: `review_decisions`
    is append-only by trigger and references it through `decision_review_fk`
    (`ON DELETE RESTRICT`-style), exactly like `invoice_memory_records`,
    `audit_events`, `workflow_instances` and `tenants`. Those cases are
    RESOLVED/REJECTED (never in the active queue) and stay behind, inert, by
    schema design; only decision-free cases (open, claimed, released) go.
    """

    from ap_agent.db.connection import open_connection, set_tenant_context

    owner_dsn = base._owner_dsn_or_fail()
    config = base._local_config()

    for tenant in args.tenant_id:
        tenant_id = uuid.UUID(tenant)
        with open_connection(owner_dsn, config) as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))
                cursor.execute(
                    "DELETE FROM ap_agent.review_cases AS review WHERE review.tenant_id = %s AND NOT EXISTS ("
                    "SELECT 1 FROM ap_agent.review_decisions AS decision "
                    "WHERE decision.tenant_id = review.tenant_id AND decision.review_id = review.review_id);",
                    (tenant_id,),
                )
                deleted = cursor.rowcount
                cursor.execute("SELECT COUNT(*) FROM ap_agent.review_cases WHERE tenant_id = %s;", (tenant_id,))
                remaining = int(cursor.fetchone()[0])
        print(
            f"Cleaned up M11C tenant {tenant_id}: deleted {deleted} decision-free review case(s); "
            f"{remaining} decided case(s) remain (append-only decisions reference them, by schema design)."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed")
    seed.add_argument("--profile", choices=("acceptance", "demo"), required=True)
    seed.add_argument("--output", required=True)
    seed.set_defaults(func=cmd_seed)

    verify = sub.add_parser("verify")
    verify.add_argument("--state", required=True)
    verify.set_defaults(func=cmd_verify)

    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--tenant-id", action="append", required=True)
    cleanup.set_defaults(func=cmd_cleanup)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

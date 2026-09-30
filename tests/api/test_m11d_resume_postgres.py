"""M11D Core resume acceptance: review decision -> durable job -> worker.

Real PostgreSQL, real FastAPI (M11C review commands), the real worker runner
and the real Phase 5/6 stage handlers. The workflow under test is seeded by
running the real Phase 5/6 tools and memory service over a hand-built
normalization result (`tests/support/m11d_seed.py`); OCR and normalization
are not part of any resume.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from ap_agent.models.normalization import InvoiceFieldName as F
from ap_agent.models.normalization import NormalizedValueType as V
from ap_agent.models.operations import WorkflowJobStatus
from tests.support.m11d_harness import Db, ReviewApi, make_client, make_runner
from tests.support.m11d_seed import seed_pipeline_workflow

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

BAD_TOTAL = {F.TOTAL_AMOUNT: (Decimal("58.71"), V.DECIMAL)}
BAD_SUPPLIER = {F.SUPPLIER_NAME: ("Unknown Vendor Ltd", V.TEXT)}


@pytest.fixture()
def client(runtime_dsn, local_config, tmp_path):
    with make_client(runtime_dsn, local_config, tmp_path / "artifacts") as test_client:
        yield test_client


@pytest.fixture()
def db(runtime_dsn, local_config, tenant_id) -> Db:
    return Db(runtime_dsn, local_config, tenant_id)


@pytest.fixture()
def runner(runtime_dsn, local_config, tenant_id, tmp_path):
    return make_runner(runtime_dsn, local_config, tenant_id=tenant_id, work_directory=tmp_path)


@pytest.fixture()
def seed(runtime_dsn, local_config, tenant_id, tmp_path):
    def build(**kwargs):
        return seed_pipeline_workflow(runtime_dsn, local_config, tenant_id=tenant_id, work_directory=tmp_path, **kwargs)

    return build


def _decide_and_resume(client, tenant_id, case_id, *, corrections=None):
    api = ReviewApi(client, tenant_id, case_id)
    api.claim()

    if corrections is None:
        api.approve()
        resumed = api.resume(disposition="APPROVED")
    else:
        api.correct(corrections(api))
        resumed = api.resume(disposition="CORRECTED")

    return api, resumed


def _job_events(runtime_dsn, local_config, tenant_id, job_id):
    from ap_agent.repositories.operations_repository import OperationsRepository

    return OperationsRepository(runtime_dsn, local_config).list_events(tenant_id, job_id)


def _stage_started(events) -> list[str]:
    return [event.stage for event in events if event.event_type == "STAGE_STARTED"]


def _header_correction(api, field, previous, corrected, *, reason="Verified against the paper invoice."):
    return {
        "field_name": field, "line_number": None, "previous_value": previous, "corrected_value": corrected,
        "reason": reason, "evidence_reference_ids": [api.evidence_id()],
    }


# ---------------------------------------------------------------- C
def test_approve_resumes_at_memory_persistence_and_appends_one_derived_version(
    client, db, runner, seed, runtime_dsn, local_config, tenant_id
):
    seeded = seed(header=BAD_TOTAL)
    assert seeded.review_required and seeded.review_case_id is not None
    original_before = db.original_memory(seeded.workflow_id)

    api, resumed = _decide_and_resume(client, tenant_id, seeded.review_case_id)
    data = resumed["data"]
    assert data["restart_stage"] == "MEMORY_PERSISTENCE"

    # The resume request committed the plan, audit event and job together.
    jobs = db.jobs(seeded.workflow_id)
    assert [(job[1], job[2]) for job in jobs] == [("RESUME_WORKFLOW", "QUEUED")]
    assert db.workflow(seeded.workflow_id)[:3] == ("MEMORY_PERSISTENCE", "IN_PROGRESS", False)
    assert db.versions(seeded.workflow_id) == []  # the HTTP request executed nothing

    finished = runner.run_once()
    assert finished is not None and finished.status == WorkflowJobStatus.SUCCEEDED

    events = _job_events(runtime_dsn, local_config, tenant_id, finished.job_id)
    assert [e.stage for e in events if e.event_type == "WORKFLOW_RESUMED"] == ["MEMORY_PERSISTENCE"]
    assert _stage_started(events) == ["MEMORY_PERSISTENCE"]  # nothing upstream ran

    summary = finished.result_summary
    assert summary["executed_stages"] == ["MEMORY_PERSISTENCE"]
    assert summary["outcome"] == "COMPLETED" and summary["restart_stage"] == "MEMORY_PERSISTENCE"

    versions = db.versions(seeded.workflow_id)
    assert len(versions) == 1
    assert versions[0][2:5] == ("MEMORY_PERSISTENCE", "SUCCEEDED", False)
    assert versions[0][6] == seeded.original_payload_sha256  # approval changes no content

    assert db.original_memory(seeded.workflow_id) == original_before  # append-only original untouched
    assert db.workflow(seeded.workflow_id)[:3] == ("COMPLETED", "SUCCEEDED", False)
    assert [case[1] for case in db.cases(seeded.workflow_id)] == ["RESOLVED"]
    assert "WORKFLOW_RESUME_EXECUTED" in db.audit_types(seeded.workflow_id)

    assert runner.run_once() is None  # nothing left to do


# ---------------------------------------------------------------- D
def test_financial_correction_restarts_at_financial_validation(
    client, db, runner, seed, runtime_dsn, local_config, tenant_id
):
    seeded = seed(header=BAD_TOTAL)
    original_before = db.original_memory(seeded.workflow_id)

    _, resumed = _decide_and_resume(
        client, tenant_id, seeded.review_case_id,
        corrections=lambda api: [_header_correction(api, "TOTAL_AMOUNT", "58.71", "48.71")],
    )
    assert resumed["data"]["restart_stage"] == "FINANCIAL_VALIDATION"

    finished = runner.run_once()
    assert finished.status == WorkflowJobStatus.SUCCEEDED, (finished.error_code, finished.result_summary)

    events = _job_events(runtime_dsn, local_config, tenant_id, finished.job_id)
    assert _stage_started(events) == ["FINANCIAL_VALIDATION", "REFERENCE_MATCHING", "MEMORY_PERSISTENCE"]
    assert finished.result_summary["corrected_fields"] == ["TOTAL_AMOUNT"]

    (version,) = db.versions(seeded.workflow_id)
    corrected_total = next(
        field for field in version[7]["invoice_record"]["fields"] if field["field_name"] == "TOTAL_AMOUNT"
    )
    assert corrected_total["normalized_value"] == "48.71"
    assert "HUMAN_REVIEW_CORRECTION" in corrected_total["normalization_notes"]

    # The correction exists only in the derived version.
    assert db.original_memory(seeded.workflow_id) == original_before
    (original_row,) = db.query(
        "SELECT normalized_invoice FROM ap_agent.invoice_memory_records WHERE tenant_id = %s AND workflow_id = %s;",
        (tenant_id, seeded.workflow_id),
    )
    original_total = next(
        field for field in original_row[0]["invoice_record"]["fields"] if field["field_name"] == "TOTAL_AMOUNT"
    )
    assert original_total["normalized_value"] == "58.71"
    assert db.workflow(seeded.workflow_id)[:3] == ("COMPLETED", "SUCCEEDED", False)


# ---------------------------------------------------------------- E
def test_reference_correction_restarts_at_reference_matching(
    client, db, runner, seed, runtime_dsn, local_config, tenant_id
):
    seeded = seed(header=BAD_SUPPLIER)
    assert seeded.review_required
    original_before = db.original_memory(seeded.workflow_id)

    _, resumed = _decide_and_resume(
        client, tenant_id, seeded.review_case_id,
        corrections=lambda api: [_header_correction(api, "SUPPLIER_NAME", "Unknown Vendor Ltd", "SuperStore")],
    )
    assert resumed["data"]["restart_stage"] == "REFERENCE_MATCHING"

    finished = runner.run_once()
    assert finished.status == WorkflowJobStatus.SUCCEEDED, (finished.error_code, finished.result_summary)

    events = _job_events(runtime_dsn, local_config, tenant_id, finished.job_id)
    assert _stage_started(events) == ["REFERENCE_MATCHING", "MEMORY_PERSISTENCE"]

    (version,) = db.versions(seeded.workflow_id)
    assert version[3] == "SUCCEEDED" and version[2] == "REFERENCE_MATCHING"
    assert finished.result_summary["supplier_status"] == "MATCHED"
    assert db.original_memory(seeded.workflow_id) == original_before


# ---------------------------------------------------------------- F
def test_a_still_failing_correction_opens_a_new_review_case_and_a_second_cycle_completes(
    client, db, runner, seed, runtime_dsn, local_config, tenant_id
):
    seeded = seed(header=BAD_SUPPLIER)
    first_case = seeded.review_case_id

    _, _ = _decide_and_resume(
        client, tenant_id, first_case,
        corrections=lambda api: [_header_correction(api, "SUPPLIER_NAME", "Unknown Vendor Ltd", "Still Unknown Co")],
    )
    finished = runner.run_once()

    # Fail closed into a visible review outcome -- never a false success.
    assert finished.status == WorkflowJobStatus.REVIEW_REQUIRED
    assert finished.result_summary["outcome"] == "RETURNED_TO_REVIEW"
    assert finished.result_review_id is not None and finished.result_review_id != first_case

    cases = db.cases(seeded.workflow_id)
    assert [case[1] for case in cases] == ["RESOLVED", "OPEN"]
    old_case, new_case = cases
    assert old_case[3] is None and new_case[3] is not None and new_case[4] is not None  # links
    assert new_case[0] == finished.result_review_id
    assert db.workflow(seeded.workflow_id)[:3] == ("HUMAN_REVIEW", "REVIEW_REQUIRED", True)

    (first_version,) = db.versions(seeded.workflow_id)
    assert first_version[3] == "REVIEW_REQUIRED" and first_version[5] == new_case[0]

    # The second case exposes the corrected (effective) value; the resolved
    # one still exposes what its reviewer saw.
    headers = {"X-Tenant-ID": str(tenant_id), "X-Actor-ID": "m11d-reviewer-1", "X-Actor-Role": "AP_REVIEWER"}
    from tests.support.m11d_harness import auth_headers
    from ap_agent.models.interface import InterfaceRole

    headers = auth_headers(tenant_id, "m11d-reviewer-1", InterfaceRole.AP_REVIEWER)

    def supplier_value(case_id):
        body = client.get(f"/api/v1/review-cases/{case_id}", headers=headers).json()["data"]
        return next(f["normalized_value"] for f in body["fields"] if f["field_name"] == "SUPPLIER_NAME")

    assert supplier_value(new_case[0]) == "Still Unknown Co"
    assert supplier_value(first_case) == "Unknown Vendor Ltd"

    # Second cycle: fix it for real -> completes from the *derived* version.
    api2 = ReviewApi(client, tenant_id, new_case[0])
    api2.claim()
    api2.correct([_header_correction(api2, "SUPPLIER_NAME", "Still Unknown Co", "SuperStore")])
    resumed = api2.resume(disposition="CORRECTED")
    assert resumed["data"]["restart_stage"] == "REFERENCE_MATCHING"

    second = runner.run_once()
    assert second.status == WorkflowJobStatus.SUCCEEDED, (second.error_code, second.result_summary)

    versions = db.versions(seeded.workflow_id)
    assert len(versions) == 2 and versions[1][8] == versions[0][0]  # chained parent version
    assert db.workflow(seeded.workflow_id)[:3] == ("COMPLETED", "SUCCEEDED", False)
    assert [case[1] for case in db.cases(seeded.workflow_id)] == ["RESOLVED", "RESOLVED"]


# ---------------------------------------------------------------- I / J
def test_resume_retries_are_idempotent(client, db, runner, seed, runtime_dsn, local_config, tenant_id):
    seeded = seed(header=BAD_TOTAL)
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    api.approve()

    body = api.body("RESUME_WORKFLOW", key="m11d-resume-key-0001", disposition="APPROVED")
    first = api.send(body)
    retry = api.send(body)  # a genuine retry re-sends the identical request

    assert first["data"]["resume_plan_id"] == retry["data"]["resume_plan_id"]
    assert retry["status"] == "IDEMPOTENT"
    assert len(db.jobs(seeded.workflow_id)) == 1
    assert db.audit_types(seeded.workflow_id).count("WORKFLOW_RESUME_REQUESTED") == 1

    # A second resume under a new key is a 409 and creates nothing.
    api.resume(disposition="APPROVED", key="m11d-resume-key-0002", expect=409)
    assert len(db.jobs(seeded.workflow_id)) == 1

    finished = runner.run_once()
    assert finished.status == WorkflowJobStatus.SUCCEEDED
    assert runner.run_once() is None
    assert len(db.versions(seeded.workflow_id)) == 1

    # Re-appending the identical derived version is a no-op (deterministic identity).
    from ap_agent.db.connection import open_connection, set_tenant_context
    from ap_agent.repositories.operations_repository import derived_version_id, insert_derived_version
    from ap_agent.services.resume_executor import verify_resume_inputs
    from ap_agent.repositories.operations_repository import OperationsRepository

    operations = OperationsRepository(runtime_dsn, local_config)
    job = operations.get_job(tenant_id, finished.job_id)

    with operations.transaction(tenant_id) as cursor:
        verified = verify_resume_inputs(cursor, job)

    assert verified.existing_version_id == derived_version_id(tenant_id, verified.resume_plan_id)
    assert len(db.versions(seeded.workflow_id)) == 1


# ---------------------------------------------------------------- reject
def test_reject_never_produces_a_resume_job(client, db, seed, tenant_id):
    seeded = seed(header=BAD_TOTAL)
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    api.command("REJECT")
    api.resume(disposition="REJECTED", expect=422)

    assert db.jobs(seeded.workflow_id) == []


# ---------------------------------------------------------------- atomicity
def test_resume_handoff_and_job_commit_or_roll_back_together(client, db, seed, tenant_id, monkeypatch):
    seeded = seed(header=BAD_TOTAL)
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    api.approve()
    before = (db.workflow(seeded.workflow_id), db.audit_types(seeded.workflow_id))

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected failure after the audit event")

    monkeypatch.setattr("ap_agent.services.workflow_resume.insert_resume_job", boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        api.resume(disposition="APPROVED")

    assert (db.workflow(seeded.workflow_id), db.audit_types(seeded.workflow_id)) == before
    assert db.jobs(seeded.workflow_id) == []


# ---------------------------------------------------------------- L
def _tamper(owner_dsn, table: str, trigger: str, sql: str, params: tuple) -> None:
    """Modify an append-only row the way only a database administrator
    could: the guarding trigger is disabled for the statement and always
    re-enabled."""

    import psycopg

    with psycopg.connect(owner_dsn) as connection:
        connection.execute(f"ALTER TABLE ap_agent.{table} DISABLE TRIGGER {trigger};")
        try:
            connection.execute(sql, params)
        finally:
            connection.execute(f"ALTER TABLE ap_agent.{table} ENABLE TRIGGER {trigger};")


@pytest.mark.parametrize(
    ("label", "expected_code"),
    [
        ("original_memory", "RESUME_ORIGINAL_MEMORY_HASH_MISMATCH"),
        ("overlay", "RESUME_OVERLAY_HASH_MISMATCH"),
        ("normalization_hash", "RESUME_NORMALIZATION_HASH_MISMATCH"),
        ("plan_identity", "RESUME_PLAN_NOT_FOUND"),
    ],
)
def test_integrity_tampering_fails_closed_before_any_stage_runs(
    label, expected_code, client, db, runner, seed, runtime_dsn, local_config, tenant_id, owner_dsn
):
    seeded = seed(header=BAD_TOTAL)
    _, resumed = _decide_and_resume(
        client, tenant_id, seeded.review_case_id,
        corrections=lambda api: [_header_correction(api, "TOTAL_AMOUNT", "58.71", "48.71")],
    )
    plan_id = resumed["data"]["resume_plan_id"]
    intact = db.original_memory(seeded.workflow_id)

    if label == "original_memory":
        _tamper(
            owner_dsn, "invoice_memory_records", "invoice_memory_no_mutation",
            "UPDATE ap_agent.invoice_memory_records SET normalized_invoice = jsonb_set(normalized_invoice, "
            "'{source_name}', '\"tampered\"') WHERE workflow_id = %s;",
            (seeded.workflow_id,),
        )
    else:
        payload_change = {
            "overlay": "jsonb_set(payload, '{correction_overlay_json}', to_jsonb(payload->>'correction_overlay_json' || ' '))",
            "normalization_hash": "jsonb_set(payload, '{source_normalization_sha256}', to_jsonb(repeat('0', 64)))",
            "plan_identity": "jsonb_set(payload, '{resume_plan_id}', to_jsonb(gen_random_uuid()::text))",
        }[label]
        _tamper(
            owner_dsn, "audit_events", "audit_events_no_mutation",
            f"UPDATE ap_agent.audit_events SET payload = {payload_change} "
            "WHERE workflow_id = %s AND event_type = 'WORKFLOW_RESUME_REQUESTED';",
            (seeded.workflow_id,),
        )

    finished = runner.run_once()

    assert finished.status == WorkflowJobStatus.FAILED and finished.error_code == expected_code
    events = _job_events(runtime_dsn, local_config, tenant_id, finished.job_id)
    assert _stage_started(events) == []  # nothing downstream executed
    assert db.versions(seeded.workflow_id) == []
    assert db.workflow(seeded.workflow_id)[1] != "SUCCEEDED"

    if label != "original_memory":
        assert db.original_memory(seeded.workflow_id) == intact
    assert plan_id  # the plan existed; the verification, not its absence, refused it

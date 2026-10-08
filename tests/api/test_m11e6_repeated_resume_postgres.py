"""M11E.6: repeated review/resume cycles (real PostgreSQL, real API, real worker, real Phase 5/6 stages).

Live defect: case A was corrected and resumed; downstream processing opened case B; case B was approved but offered no
Workflow-resume control, because ANY earlier `WORKFLOW_RESUME_REQUESTED` event in the workflow made it "already requested".
The guard is now scoped to the current decision of the current review case."""

from __future__ import annotations

import json
import uuid

import pytest

from ap_agent.models.normalization import InvoiceFieldName as F
from ap_agent.models.normalization import NormalizedValueType as V
from ap_agent.models.operations import WorkflowJobStatus
from ap_agent.repositories.operations_repository import OperationsRepository
from ap_agent.repositories.review_repository import ReviewRepository
from tests.support.m11d_harness import Db, ReviewApi, auth_headers, make_client, make_runner
from tests.support.m11d_seed import seed_pipeline_workflow

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

BAD_SUPPLIER = {F.SUPPLIER_NAME: ("Unknown Vendor Ltd", V.TEXT)}
EARLY_STAGES = {"INGESTION", "PREPROCESSING", "OCR", "OCR_EXTRACTION", "NORMALIZATION", "DOCUMENT_INGESTION"}


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


def _seed(runtime_dsn, local_config, tenant, tmp_path, **kwargs):
    return seed_pipeline_workflow(runtime_dsn, local_config, tenant_id=tenant, work_directory=tmp_path, **kwargs)


def _correction(api, previous, corrected):
    return {
        "field_name": "SUPPLIER_NAME", "line_number": None, "previous_value": previous, "corrected_value": corrected,
        "reason": "Verified against the paper invoice.", "evidence_reference_ids": [api.evidence_id()],
    }


def _resume(caps):
    return caps["resume"]


def _stages(runtime_dsn, local_config, tenant_id, job_id) -> list[str]:
    events = OperationsRepository(runtime_dsn, local_config).list_events(tenant_id, job_id)
    return [event.stage for event in events if event.event_type == "STAGE_STARTED"]


def _snapshot(db, workflow_id):
    decisions = db.query(
        "SELECT decision_id, review_id, decision_type, decided_by, evidence::text FROM ap_agent.review_decisions "
        "WHERE tenant_id = %s AND workflow_id = %s ORDER BY decided_at, decision_id;", (db.tenant_id, workflow_id),
    )
    audit = db.query(
        "SELECT sequence_number, event_type, payload::text FROM ap_agent.audit_events WHERE tenant_id = %s AND workflow_id = %s "
        "ORDER BY sequence_number;", (db.tenant_id, workflow_id),
    )
    return decisions, audit


def test_the_complete_repeated_cycle_correction_resume_downstream_review_approval_second_resume(
    client, db, runner, runtime_dsn, local_config, tenant_id, tmp_path
):
    seeded = _seed(runtime_dsn, local_config, tenant_id, tmp_path, header=BAD_SUPPLIER)
    original_before = db.original_memory(seeded.workflow_id)

    # ---- case A: corrected and resumed (the existing single-cycle behaviour) --------------------------------------------------
    api_a = ReviewApi(client, tenant_id, seeded.review_case_id)
    api_a.claim()
    api_a.correct([_correction(api_a, "Unknown Vendor Ltd", "Still Unknown Co")])
    assert _resume(api_a.caps())["eligible"] is True and _resume(api_a.caps())["already_requested"] is False

    first = api_a.resume(disposition="CORRECTED", key="m11e6-resume-case-a-1")
    plan_a = first["data"]["resume_plan_id"]
    assert _resume(api_a.caps())["already_requested"] is True
    assert _resume(api_a.caps())["ineligible_reason"] == "RESUME_ALREADY_REQUESTED"

    job_a = runner.run_once()
    assert job_a.status == WorkflowJobStatus.REVIEW_REQUIRED and job_a.result_summary["outcome"] == "RETURNED_TO_REVIEW"

    # ---- downstream case B opened by the resumed stages ---------------------------------------------------------------------------
    cases = db.cases(seeded.workflow_id)
    assert [case[1] for case in cases] == ["RESOLVED", "OPEN"]
    case_b = cases[1][0]
    assert case_b == job_a.result_review_id and case_b != seeded.review_case_id and cases[1][4] is not None
    assert db.workflow(seeded.workflow_id)[:3] == ("HUMAN_REVIEW", "REVIEW_REQUIRED", True)

    api_b = ReviewApi(client, tenant_id, case_b)
    assert _resume(api_b.caps())["eligible"] is False and _resume(api_b.caps())["ineligible_reason"] == "REVIEW_DECISION_REQUIRED"
    assert _resume(api_a.caps())["already_requested"] is True  # case A is unaffected by case B

    api_b.claim()
    api_b.approve()

    # ---- THE REGRESSION: case B is resumable although case A's resume exists in the same workflow --------------------------------
    caps_b = api_b.caps()
    assert _resume(caps_b)["eligible"] is True and _resume(caps_b)["already_requested"] is False
    assert _resume(caps_b)["ineligible_reason"] is None and _resume(caps_b)["disposition"] == "APPROVED"
    assert any(e["event_type"] == "WORKFLOW_RESUME_REQUESTED" for e in client.get(
        f"/api/v1/review-cases/{case_b}", headers=auth_headers(tenant_id, "m11d-reviewer-1", api_b.role)
    ).json()["data"]["timeline"])  # the workflow timeline DOES hold case A's resume event, and it no longer matters

    snapshot_before_second = _snapshot(db, seeded.workflow_id)
    body_b = api_b.body("RESUME_WORKFLOW", key="m11e6-resume-case-b-1", disposition="APPROVED")
    second = api_b.send(body_b)
    plan_b = second["data"]["resume_plan_id"]
    assert plan_b != plan_a and second["data"]["restart_stage"] == "MEMORY_PERSISTENCE"

    jobs = db.jobs(seeded.workflow_id)
    assert [(job[1], job[2]) for job in jobs] == [("RESUME_WORKFLOW", "REVIEW_REQUIRED"), ("RESUME_WORKFLOW", "QUEUED")]
    assert jobs[0][0] != jobs[1][0]  # distinct durable jobs
    assert db.audit_types(seeded.workflow_id).count("WORKFLOW_RESUME_REQUESTED") == 2

    # the second resume is now recorded against case B's decision only
    after = api_b.caps()
    assert _resume(after)["already_requested"] is True and _resume(after)["ineligible_reason"] == "RESUME_ALREADY_REQUESTED"

    # ---- duplicates are blocked: new key -> conflict, nothing created; same key -> idempotent, same plan, no new job --------------
    api_b.resume(disposition="APPROVED", key="m11e6-resume-case-b-2", expect=409)
    replay = api_b.send(body_b)  # byte-identical body: the same command
    assert replay["status"] == "IDEMPOTENT" and replay["data"]["resume_plan_id"] == plan_b
    assert len(db.jobs(seeded.workflow_id)) == 2
    assert db.audit_types(seeded.workflow_id).count("WORKFLOW_RESUME_REQUESTED") == 2

    # ---- the worker completes the workflow from the derived version; nothing upstream reruns -------------------------------------
    job_b = runner.run_once()
    assert job_b.status == WorkflowJobStatus.SUCCEEDED, (job_b.error_code, job_b.result_summary)
    assert job_b.job_id != job_a.job_id
    assert _stages(runtime_dsn, local_config, tenant_id, job_b.job_id) == ["MEMORY_PERSISTENCE"]

    for job in (job_a, job_b):
        started = set(_stages(runtime_dsn, local_config, tenant_id, job.job_id))
        assert not (started & EARLY_STAGES), started  # no ingestion/preprocessing/OCR/normalisation rerun

    assert db.workflow(seeded.workflow_id)[:3] == ("COMPLETED", "SUCCEEDED", False)
    assert [case[1] for case in db.cases(seeded.workflow_id)] == ["RESOLVED", "RESOLVED"]
    versions = db.versions(seeded.workflow_id)
    assert len(versions) == 2 and versions[1][8] == versions[0][0]  # chained derived versions
    assert versions[0][1] != versions[1][1]  # distinct derived-version labels
    assert runner.run_once() is None

    # ---- immutability ------------------------------------------------------------------------------------------------------------
    assert db.original_memory(seeded.workflow_id) == original_before
    decisions_after, audit_after = _snapshot(db, seeded.workflow_id)
    decisions_before, audit_before = snapshot_before_second
    assert decisions_after[: len(decisions_before)] == decisions_before
    assert audit_after[: len(audit_before)] == audit_before  # earlier audit events were never modified
    assert len(decisions_after) == 2  # one append-only decision per case


def test_the_same_case_cannot_be_resumed_twice_and_replays_create_nothing(client, db, runner, runtime_dsn, local_config, tenant_id, tmp_path):
    seeded = _seed(runtime_dsn, local_config, tenant_id, tmp_path, header=BAD_SUPPLIER)
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    api.correct([_correction(api, "Unknown Vendor Ltd", "SuperStore")])
    body = api.body("RESUME_WORKFLOW", key="m11e6-single-1", disposition="CORRECTED")
    first = api.send(body)

    assert api.send(body)["status"] == "IDEMPOTENT"
    api.resume(disposition="CORRECTED", key="m11e6-single-2", expect=409)
    assert len(db.jobs(seeded.workflow_id)) == 1 and db.audit_types(seeded.workflow_id).count("WORKFLOW_RESUME_REQUESTED") == 1
    assert _resume(api.caps())["eligible"] is False and first["data"]["resume_plan_id"]

    assert runner.run_once().status == WorkflowJobStatus.SUCCEEDED
    assert _resume(api.caps())["already_requested"] is True  # single-cycle behaviour unchanged


def test_other_workflows_and_tenants_resumes_never_grant_or_block_eligibility(client, db, runner, runtime_dsn, local_config, tenant_id, tmp_path):
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    # our workflow, case A resumed -> case B approved
    ours = _seed(runtime_dsn, local_config, tenant_id, tmp_path, header=BAD_SUPPLIER)
    api_a = ReviewApi(client, tenant_id, ours.review_case_id)
    api_a.claim()
    api_a.correct([_correction(api_a, "Unknown Vendor Ltd", "Still Unknown Co")])
    api_a.resume(disposition="CORRECTED")
    case_b = runner.run_once().result_review_id
    api_b = ReviewApi(client, tenant_id, case_b)
    api_b.claim()
    api_b.approve()
    assert _resume(api_b.caps())["eligible"] is True

    # a DIFFERENT workflow in the same tenant: resumed in full
    neighbour = _seed(runtime_dsn, local_config, tenant_id, tmp_path, header=BAD_SUPPLIER)
    api_n = ReviewApi(client, tenant_id, neighbour.review_case_id)
    api_n.claim()
    api_n.approve()
    api_n.resume(disposition="APPROVED")

    # a DIFFERENT tenant: its own workflow resumed too
    other_tenant = uuid.uuid4()
    PostgresMemoryRepository(runtime_dsn, local_config).register_tenant(
        tenant_id=other_tenant, tenant_key=f"m11e6-{other_tenant.hex[:8]}", display_name="Other tenant"
    )
    foreign = _seed(runtime_dsn, local_config, other_tenant, tmp_path, header=BAD_SUPPLIER)
    api_f = ReviewApi(client, other_tenant, foreign.review_case_id)
    api_f.claim()
    api_f.approve()
    api_f.resume(disposition="APPROVED")

    # case B: still eligible; its context names only its own decisions, never a neighbour's or a foreign one
    assert _resume(api_b.caps())["eligible"] is True and _resume(api_b.caps())["already_requested"] is False
    context = ReviewRepository(runtime_dsn, local_config).get_command_context(tenant_id, case_b)
    own = [row[0] for row in db.query("SELECT decision_id::text FROM ap_agent.review_decisions WHERE tenant_id = %s AND review_id = %s;", (tenant_id, case_b))]
    assert context.resume_requested_decision_ids == () and len(own) == 1

    # the neighbour and foreign cases are individually blocked by THEIR OWN resume, and nothing leaks across
    neighbour_context = ReviewRepository(runtime_dsn, local_config).get_command_context(tenant_id, neighbour.review_case_id)
    assert len(neighbour_context.resume_requested_decision_ids) == 1
    assert not set(neighbour_context.resume_requested_decision_ids) & set(context.resume_requested_decision_ids)
    assert ReviewRepository(runtime_dsn, local_config).get_command_context(tenant_id, foreign.review_case_id) is None  # RLS: invisible
    assert api_f.caps()["resume"]["already_requested"] is True

    # and case B can really be resumed now
    assert api_b.resume(disposition="APPROVED", key="m11e6-isolation-b-1")["status"] == "ACCEPTED"


def test_the_second_case_of_a_workflow_with_a_correction_decision_is_also_resumable(client, db, runner, runtime_dsn, local_config, tenant_id, tmp_path):
    # case B corrected (not approved) and resumed -> a third, distinct plan: repeated cycles are not limited to two
    seeded = _seed(runtime_dsn, local_config, tenant_id, tmp_path, header=BAD_SUPPLIER)
    api_a = ReviewApi(client, tenant_id, seeded.review_case_id)
    api_a.claim()
    api_a.correct([_correction(api_a, "Unknown Vendor Ltd", "Still Unknown Co")])
    plan_a = api_a.resume(disposition="CORRECTED")["data"]["resume_plan_id"]
    case_b = runner.run_once().result_review_id

    api_b = ReviewApi(client, tenant_id, case_b)
    api_b.claim()
    api_b.correct([_correction(api_b, "Still Unknown Co", "SuperStore")])
    assert _resume(api_b.caps())["eligible"] is True
    plan_b = api_b.resume(disposition="CORRECTED")["data"]["resume_plan_id"]

    assert plan_a != plan_b and runner.run_once().status == WorkflowJobStatus.SUCCEEDED
    assert len(db.jobs(seeded.workflow_id)) == 2

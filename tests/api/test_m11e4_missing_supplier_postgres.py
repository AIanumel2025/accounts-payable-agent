"""M11E.4: the complete hosted review flow for a MISSING supplier -- claim -> correct (previous_value null) -> resume -> worker.

Real PostgreSQL, real FastAPI review-command API, the real worker runner and the real Phase 5/6 stages (the same harness as
`test_m11d_resume_postgres.py`). The workflow is seeded WITHOUT any SUPPLIER_NAME field, as the live case was."""

from __future__ import annotations

import pytest

from ap_agent.models.normalization import InvoiceFieldName as F
from ap_agent.models.operations import WorkflowJobStatus
from tests.support.m11d_harness import Db, ReviewApi, make_client, make_runner
from tests.support.m11d_seed import seed_pipeline_workflow

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


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
def seeded(runtime_dsn, local_config, tenant_id, tmp_path):
    return seed_pipeline_workflow(
        runtime_dsn, local_config, tenant_id=tenant_id, work_directory=tmp_path, omit_header=(F.SUPPLIER_NAME,)
    )


def _supplier_correction(api, *, previous=None, corrected="SuperStore", **overrides):
    return {
        "field_name": "SUPPLIER_NAME", "line_number": None, "previous_value": previous, "corrected_value": corrected,
        "reason": "Read from the paper invoice.", "evidence_reference_ids": [api.evidence_id()], **overrides,
    }


def _stored_fields(db, workflow_id) -> dict[str, dict]:
    (row,) = db.query(
        "SELECT normalized_invoice FROM ap_agent.invoice_memory_records WHERE tenant_id = %s AND workflow_id = %s;",
        (db.tenant_id, workflow_id),
    )
    return {field["field_name"]: field for field in row[0]["invoice_record"]["fields"]}


def test_the_seeded_case_is_missing_the_supplier_and_needs_review(seeded, db):
    assert seeded.review_required and seeded.review_case_id is not None
    assert "SUPPLIER_NAME" not in _stored_fields(db, seeded.workflow_id)


def test_a_missing_supplier_appears_as_an_editable_target(client, seeded, tenant_id):
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    headers = {item["field_name"]: item["current_value"] for item in api.caps()["correction_policy"]["header_fields"]}

    assert "SUPPLIER_NAME" in headers and headers["SUPPLIER_NAME"] is None
    assert headers["INVOICE_NUMBER"] == "INV-1001"  # present fields keep their exact current value


def test_the_complete_flow_claim_correct_missing_supplier_resume(client, db, runner, seeded, runtime_dsn, local_config, tenant_id):
    from ap_agent.repositories.operations_repository import OperationsRepository

    original_before = db.original_memory(seeded.workflow_id)
    original_fields_before = _stored_fields(db, seeded.workflow_id)

    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    corrected = api.correct([_supplier_correction(api)])
    assert corrected["status"] == "ACCEPTED"
    resumed = api.resume(disposition="CORRECTED")

    # resume begins at the recorded restart stage for a supplier correction
    assert resumed["data"]["restart_stage"] == "REFERENCE_MATCHING"

    finished = runner.run_once()
    assert finished is not None and finished.status == WorkflowJobStatus.SUCCEEDED, (finished.error_code, finished.result_summary)
    assert finished.result_summary["corrected_fields"] == ["SUPPLIER_NAME"]

    events = OperationsRepository(runtime_dsn, local_config).list_events(tenant_id, finished.job_id)
    assert [e.stage for e in events if e.event_type == "STAGE_STARTED"] == ["REFERENCE_MATCHING", "MEMORY_PERSISTENCE"]

    # downstream validation consumed the supplier: the reference match used the corrected value
    assert finished.result_summary["supplier_status"] == "MATCHED"

    # the derived version contains the corrected supplier, with human-review provenance
    (version,) = db.versions(seeded.workflow_id)
    assert version[2] == "REFERENCE_MATCHING" and version[3] == "SUCCEEDED"
    supplier = next(f for f in version[7]["invoice_record"]["fields"] if f["field_name"] == "SUPPLIER_NAME")
    assert supplier["normalized_value"] == "SuperStore" and supplier["value_type"] == "TEXT"
    assert supplier["confidence"] == 100 and supplier["review_required"] is False
    assert "HUMAN_REVIEW_CORRECTION" in supplier["normalization_notes"]
    assert "previous_value=<missing>" in supplier["normalization_notes"]

    # the original invoice_memory_records entry is untouched (append-only)
    assert db.original_memory(seeded.workflow_id) == original_before
    assert _stored_fields(db, seeded.workflow_id) == original_fields_before and "SUPPLIER_NAME" not in original_fields_before

    assert db.workflow(seeded.workflow_id)[:3] == ("COMPLETED", "SUCCEEDED", False)
    assert [case[1] for case in db.cases(seeded.workflow_id)] == ["RESOLVED"]
    assert runner.run_once() is None


def test_a_forged_previous_value_for_the_missing_supplier_is_rejected_without_mutation(client, db, seeded, tenant_id):
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    before = (db.workflow(seeded.workflow_id), db.cases(seeded.workflow_id), db.original_memory(seeded.workflow_id))

    response = api.command("CORRECT", corrections=[_supplier_correction(api, previous="Acme Ltd")], expect=422)

    assert "PREVIOUS_VALUE_MISMATCH" in response["errors"] or "PREVIOUS_VALUE_MISMATCH" in str(response)
    assert (db.workflow(seeded.workflow_id), db.cases(seeded.workflow_id), db.original_memory(seeded.workflow_id)) == before


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"field_name": "ACCOUNT_NUMBER"}, "REQUEST_VALIDATION_FAILED"),  # arbitrary field
        ({"line_number": 1}, "HEADER_CORRECTION_LINE_NUMBER_PROHIBITED"),
        ({"evidence_reference_ids": ["ev-not-real"]}, "UNKNOWN_EVIDENCE_REFERENCE"),
        ({"corrected_value": "   "}, "CORRECTED_VALUE_MISSING"),
    ],
)
def test_other_invalid_supplier_corrections_are_still_rejected(client, db, seeded, tenant_id, override, code):
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    response = api.command("CORRECT", corrections=[_supplier_correction(api, **override)], expect=422)

    assert code in str(response)


def test_duplicate_targets_and_a_stale_revision_are_rejected(client, seeded, tenant_id):
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    duplicate = api.command("CORRECT", corrections=[_supplier_correction(api), _supplier_correction(api, corrected="Other")], expect=422)
    stale = api.command("CORRECT", corrections=[_supplier_correction(api)], observed_review_revision=999, expect=409)

    assert "DUPLICATE_CORRECTION_TARGET" in str(duplicate) and "STALE_REVIEW_REVISION" in str(stale)

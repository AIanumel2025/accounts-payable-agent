"""M11E.5: the labelled, case-bound source-document evidence, end to end (real PostgreSQL, real API, real worker).

The live defect: the *Supporting evidence* selector showed opaque UUIDs only, and for a value the extractor never found (a
missing supplier) there was nothing sensible to cite. The correction below cites ONLY the original source invoice."""

from __future__ import annotations

import json
import uuid

import pytest

from ap_agent.models.normalization import InvoiceFieldName as F
from ap_agent.models.operations import WorkflowJobStatus
from ap_agent.models.review_evidence import SOURCE_DOCUMENT_LABEL, source_document_evidence_id
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


def _seed(runtime_dsn, local_config, tenant_id, tmp_path, **kwargs):
    return seed_pipeline_workflow(runtime_dsn, local_config, tenant_id=tenant_id, work_directory=tmp_path, **kwargs)


@pytest.fixture()
def seeded(runtime_dsn, local_config, tenant_id, tmp_path):
    return _seed(runtime_dsn, local_config, tenant_id, tmp_path, omit_header=(F.SUPPLIER_NAME,))


def _source_id(seeded) -> str:
    return source_document_evidence_id(seeded.document_id, seeded.source_sha256)


def _correction(evidence, *, previous=None, corrected="SuperStore"):
    return {
        "field_name": "SUPPLIER_NAME", "line_number": None, "previous_value": previous, "corrected_value": corrected,
        "reason": "Read from the paper invoice.", "evidence_reference_ids": list(evidence),
    }


def _state(db, seeded):
    return (db.workflow(seeded.workflow_id), db.cases(seeded.workflow_id), db.original_memory(seeded.workflow_id), db.audit_types(seeded.workflow_id))


# -- the API payload -------------------------------------------------------------------------------------------------------------------


def test_capabilities_return_the_labelled_source_evidence_first(client, seeded, tenant_id):
    policy = ReviewApi(client, tenant_id, seeded.review_case_id).caps()["correction_policy"]
    options = policy["evidence_options"]

    assert options[0] == {
        "reference_id": _source_id(seeded), "evidence_type": "SOURCE_DOCUMENT", "label": SOURCE_DOCUMENT_LABEL,
        "page_number": None, "snippet": None,
    }
    assert _source_id(seeded) in policy["evidence_reference_ids"]
    assert {o["reference_id"] for o in options} == set(policy["evidence_reference_ids"])
    extracted = [o for o in options if o["evidence_type"] == "EXTRACTED_FIELD"]
    assert extracted and all(o["label"].startswith("Extracted evidence") for o in extracted)
    assert any(o["page_number"] == 1 and o["snippet"] for o in extracted)  # a page and a safe snippet are returned when stored


def test_the_api_payload_exposes_no_location_credential_hash_or_tenant(client, seeded, tenant_id):
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    text = json.dumps(api.caps())

    for forbidden in (
        str(tenant_id), seeded.source_sha256, "s3://", "object://", "artifact://", "tenants/", "staging/", "/tmp", "amazonaws",
        "postgres", "AKIA", "X-Tenant",
    ):
        assert forbidden not in text, forbidden


# -- the complete flow, citing only the source invoice ----------------------------------------------------------------------------------------


def test_missing_supplier_corrected_with_source_evidence_then_resumed(client, db, runner, seeded, runtime_dsn, local_config, tenant_id):
    from ap_agent.repositories.operations_repository import OperationsRepository

    original_before = db.original_memory(seeded.workflow_id)
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()

    accepted = api.correct([_correction([_source_id(seeded)])])
    assert accepted["status"] == "ACCEPTED"

    # the append-only decision and audit records hold only the opaque reference id -- never the label, a location or the hash
    decision = db.query(
        "SELECT evidence FROM ap_agent.review_decisions WHERE tenant_id = %s AND workflow_id = %s;", (tenant_id, seeded.workflow_id)
    )
    audit = db.query(
        "SELECT payload FROM ap_agent.audit_events WHERE tenant_id = %s AND workflow_id = %s;", (tenant_id, seeded.workflow_id)
    )
    (decision_evidence,) = [row[0] for row in decision]
    assert decision_evidence["corrections"][0]["evidence_reference_ids"] == [_source_id(seeded)]
    stored = json.dumps([decision_evidence] + [row[0] for row in audit])
    assert _source_id(seeded) in stored
    for forbidden in (SOURCE_DOCUMENT_LABEL, "SHA-256", seeded.source_sha256, "s3://", "tenants/", "amazonaws"):
        assert forbidden not in stored, forbidden

    resumed = api.resume(disposition="CORRECTED")
    assert resumed["data"]["restart_stage"] == "REFERENCE_MATCHING"

    finished = runner.run_once()
    assert finished is not None and finished.status == WorkflowJobStatus.SUCCEEDED, (finished.error_code, finished.result_summary)
    assert finished.result_summary["corrected_fields"] == ["SUPPLIER_NAME"] and finished.result_summary["supplier_status"] == "MATCHED"

    events = OperationsRepository(runtime_dsn, local_config).list_events(tenant_id, finished.job_id)
    assert [e.stage for e in events if e.event_type == "STAGE_STARTED"] == ["REFERENCE_MATCHING", "MEMORY_PERSISTENCE"]

    (version,) = db.versions(seeded.workflow_id)
    supplier = next(f for f in version[7]["invoice_record"]["fields"] if f["field_name"] == "SUPPLIER_NAME")
    assert supplier["normalized_value"] == "SuperStore" and "HUMAN_REVIEW_CORRECTION" in supplier["normalization_notes"]
    assert any(note == f"evidence_reference_ids={_source_id(seeded)}" for note in supplier["normalization_notes"])

    assert db.original_memory(seeded.workflow_id) == original_before  # append-only original untouched
    assert db.workflow(seeded.workflow_id)[:3] == ("COMPLETED", "SUCCEEDED", False)


def test_extracted_field_evidence_stays_valid(client, db, seeded, tenant_id):
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    extracted = next(o for o in api.caps()["correction_policy"]["evidence_options"] if o["evidence_type"] == "EXTRACTED_FIELD")

    assert api.correct([_correction([extracted["reference_id"]])])["status"] == "ACCEPTED"


def test_source_and_extracted_evidence_can_be_cited_together(client, seeded, tenant_id):
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    extracted = next(o for o in api.caps()["correction_policy"]["evidence_options"] if o["evidence_type"] == "EXTRACTED_FIELD")

    assert api.correct([_correction([_source_id(seeded), extracted["reference_id"]])])["status"] == "ACCEPTED"


# -- forged / mismatched evidence is rejected without mutation -------------------------------------------------------------------------------


def test_forged_and_free_form_evidence_ids_are_rejected_without_mutation(client, db, seeded, tenant_id):
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    before = _state(db, seeded)

    for forged in (str(uuid.uuid4()), "ev-forged", seeded.source_sha256, str(seeded.document_id), "SOURCE_DOCUMENT"):
        response = api.command("CORRECT", corrections=[_correction([forged])], expect=422)
        assert "UNKNOWN_EVIDENCE_REFERENCE" in str(response), forged

    mixed = api.command("CORRECT", corrections=[_correction([_source_id(seeded), "ev-forged"])], expect=422)
    assert "UNKNOWN_EVIDENCE_REFERENCE" in str(mixed)
    assert _state(db, seeded) == before


def test_evidence_is_still_required(client, db, seeded, tenant_id):
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    before = _state(db, seeded)

    response = api.command("CORRECT", corrections=[_correction([])], expect=422)

    assert "CORRECTION_EVIDENCE_REQUIRED" in str(response) and _state(db, seeded) == before


def test_another_cases_source_evidence_is_rejected_without_mutation(client, db, seeded, runtime_dsn, local_config, tenant_id, tmp_path):
    other = _seed(runtime_dsn, local_config, tenant_id, tmp_path, omit_header=(F.SUPPLIER_NAME,))  # same tenant, another document
    assert _source_id(other) != _source_id(seeded)

    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    before = _state(db, seeded)

    response = api.command("CORRECT", corrections=[_correction([_source_id(other)])], expect=422)

    assert "UNKNOWN_EVIDENCE_REFERENCE" in str(response) and _state(db, seeded) == before
    assert _source_id(other) not in json.dumps(api.caps())  # and it is not offered here either


def test_a_same_document_with_a_different_stored_hash_derives_a_different_id(seeded):
    assert source_document_evidence_id(seeded.document_id, "f" * 64) != _source_id(seeded)


def test_another_tenants_source_evidence_is_rejected_and_its_case_is_invisible(client, db, seeded, runtime_dsn, local_config, tenant_id, tmp_path):
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    other_tenant = uuid.uuid4()
    PostgresMemoryRepository(runtime_dsn, local_config).register_tenant(
        tenant_id=other_tenant, tenant_key=f"m11e5-{other_tenant.hex[:8]}", display_name="Other tenant"
    )
    foreign = _seed(runtime_dsn, local_config, other_tenant, tmp_path, omit_header=(F.SUPPLIER_NAME,))

    # the foreign case is not visible to this tenant ...
    from tests.support.m11d_harness import auth_headers
    from ap_agent.models.interface import InterfaceRole

    hidden = client.get(
        f"/api/v1/review-cases/{foreign.review_case_id}/command-capabilities",
        headers=auth_headers(tenant_id, "rev-1", InterfaceRole.AP_REVIEWER),
    )
    assert hidden.status_code == 404

    # ... and its source evidence id is not accepted on this tenant's case, nor is anything mutated
    api = ReviewApi(client, tenant_id, seeded.review_case_id)
    api.claim()
    before = _state(db, seeded)
    response = api.command("CORRECT", corrections=[_correction([_source_id(foreign)])], expect=422)

    assert "UNKNOWN_EVIDENCE_REFERENCE" in str(response) and _state(db, seeded) == before

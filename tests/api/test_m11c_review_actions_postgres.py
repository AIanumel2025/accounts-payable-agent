"""M11C `requires_postgres` acceptance suite for the interactive review actions.

Every scenario drives the real FastAPI app (write-enabled or
validation-only, as the scenario requires) through `TestClient` against the
isolated `ap_agent_m8_test` database as a freshly created least-privilege
role, inside a fresh per-test tenant (`tests/api/conftest.py`), and then
verifies the resulting state **directly in PostgreSQL** -- decision rows,
audit events, workflow lock versions, original-memory hash and payload --
rather than trusting the API's own response (M11C task §24/§28).

Same DSN gate as the other `requires_postgres` suites: reads only
`AP_AGENT_TEST_POSTGRES_DSN`, fails closed unless it names
`ap_agent_m8_test`, and never prints a credential.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from ap_agent.api.config import ApiConfig
from ap_agent.models.interface import InterfaceRole, interface_utc_now

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

REVIEWER = "m11c-reviewer-1"
OTHER_REVIEWER = "m11c-reviewer-2"


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------


def _make_client(runtime_dsn, local_config, *, writes: bool, raise_server_exceptions: bool = True) -> TestClient:
    from ap_agent.api.app import create_app

    app = create_app(
        dsn=runtime_dsn, memory_config=local_config, api_config=ApiConfig(enable_review_command_writes=writes)
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _headers(tenant_id: uuid.UUID, actor_id: str, role: InterfaceRole) -> dict[str, str]:
    from datetime import timedelta

    return {
        "X-Tenant-ID": str(tenant_id),
        "X-Actor-ID": actor_id,
        "X-Actor-Role": role.value,
        "X-Authenticated-At": (interface_utc_now() - timedelta(seconds=5)).isoformat(),
    }


@dataclass
class Db:
    """Direct, tenant-scoped PostgreSQL inspection (runtime role, RLS on)."""

    dsn: str = field(repr=False)  # never let a credential reach a failure report
    config: Any = field(repr=False)
    tenant_id: uuid.UUID

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        from ap_agent.db.connection import open_connection, set_tenant_context

        with open_connection(self.dsn, self.config) as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(self.tenant_id))
                cursor.execute(sql, params)
                return cursor.fetchall()

    def decisions(self, case_id) -> list[tuple]:
        return self.query(
            "SELECT decision_id, decision_type, decided_by, evidence FROM ap_agent.review_decisions "
            "WHERE tenant_id = %s AND review_id = %s ORDER BY decided_at, decision_id;",
            (self.tenant_id, case_id),
        )

    def audit_count(self, workflow_id, event_type: Optional[str] = None) -> int:
        if event_type is None:
            rows = self.query(
                "SELECT COUNT(*) FROM ap_agent.audit_events WHERE tenant_id = %s AND workflow_id = %s;",
                (self.tenant_id, workflow_id),
            )
        else:
            rows = self.query(
                "SELECT COUNT(*) FROM ap_agent.audit_events WHERE tenant_id = %s AND workflow_id = %s "
                "AND event_type = %s;",
                (self.tenant_id, workflow_id, event_type),
            )
        return int(rows[0][0])

    def case(self, case_id) -> tuple:
        return self.query(
            "SELECT review_status, assigned_to, resolution_code FROM ap_agent.review_cases "
            "WHERE tenant_id = %s AND review_id = %s;",
            (self.tenant_id, case_id),
        )[0]

    def lock_version(self, workflow_id) -> int:
        return int(
            self.query(
                "SELECT lock_version FROM ap_agent.workflow_instances WHERE tenant_id = %s AND workflow_id = %s;",
                (self.tenant_id, workflow_id),
            )[0][0]
        )

    def memory(self, workflow_id) -> tuple[str, str]:
        import json

        row = self.query(
            "SELECT payload_sha256, normalized_invoice FROM ap_agent.invoice_memory_records "
            "WHERE tenant_id = %s AND workflow_id = %s;",
            (self.tenant_id, workflow_id),
        )[0]
        return row[0], json.dumps(row[1], sort_keys=True)

    def total_state(self) -> tuple:
        """Whole-tenant fingerprint used to prove *no* mutation happened."""

        return (
            self.query("SELECT COUNT(*) FROM ap_agent.review_decisions WHERE tenant_id = %s;", (self.tenant_id,)),
            self.query("SELECT COUNT(*) FROM ap_agent.audit_events WHERE tenant_id = %s;", (self.tenant_id,)),
            self.query(
                "SELECT review_id, review_status, assigned_to FROM ap_agent.review_cases "
                "WHERE tenant_id = %s ORDER BY review_id;",
                (self.tenant_id,),
            ),
            self.query(
                "SELECT workflow_id, lock_version FROM ap_agent.workflow_instances "
                "WHERE tenant_id = %s ORDER BY workflow_id;",
                (self.tenant_id,),
            ),
        )


class Api:
    def __init__(self, client: TestClient, tenant_id: uuid.UUID, case, actor_id: str = REVIEWER,
                 role: InterfaceRole = InterfaceRole.AP_REVIEWER):
        self.client = client
        self.tenant_id = tenant_id
        self.case = case
        self.actor_id = actor_id
        self.role = role

    def as_actor(self, actor_id: str, role: InterfaceRole = InterfaceRole.AP_REVIEWER) -> "Api":
        return Api(self.client, self.tenant_id, self.case, actor_id, role)

    def caps(self) -> dict:
        response = self.client.get(
            f"/api/v1/review-cases/{self.case.review_case_id}/command-capabilities",
            headers=_headers(self.tenant_id, self.actor_id, self.role),
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def body(self, action: str, /, *, key: str, caps: Optional[dict] = None, **overrides: Any) -> dict:
        caps = caps or self.caps()
        disposition = {"ACCEPT": "APPROVED", "CORRECT": "CORRECTED", "REJECT": "REJECTED"}.get(action)
        body = {
            "command_id": str(uuid.uuid4()),
            "idempotency_key": key,
            "action": action,
            "disposition": disposition,
            "observed_review_revision": caps["review_revision"],
            "observed_workflow_revision": caps["workflow_revision"],
            "reason_codes": [] if action in {"CLAIM", "RELEASE"} else ["REVIEWER_DECISION"],
            "notes": "Reviewed." if action == "REJECT" else None,
            "corrections": [],
            "requested_at": interface_utc_now().isoformat(),
        }
        body.update(overrides)
        return body

    def post(self, body: dict, *, actor_id: Optional[str] = None, role: Optional[InterfaceRole] = None):
        return self.client.post(
            f"/api/v1/review-cases/{self.case.review_case_id}/commands",
            headers=_headers(self.tenant_id, actor_id or self.actor_id, role or self.role),
            json=body,
        )

    def run(self, action: str, /, *, key: str, expect: int = 200, **overrides: Any) -> dict:
        response = self.post(self.body(action, key=key, **overrides))
        assert response.status_code == expect, response.text
        return response.json()

    def resume(self, *, key: str, disposition: str, expect: int = 200) -> dict:
        return self.run("RESUME_WORKFLOW", key=key, disposition=disposition, expect=expect)


def _line_correction(evidence: tuple[str, ...] = ("ev-po-1",), **overrides: Any) -> dict:
    correction = {
        "field_name": "LINE_QUANTITY",
        "line_number": 1,
        "previous_value": "5",
        "corrected_value": "6",
        "reason": "Quantity verified against the delivery note.",
        "evidence_reference_ids": list(evidence),
    }
    correction.update(overrides)
    return correction


def _header_correction(**overrides: Any) -> dict:
    correction = {
        "field_name": "PURCHASE_ORDER_NUMBER",
        "line_number": None,
        "previous_value": "99",
        "corrected_value": "99A",
        "reason": "PO number verified.",
        "evidence_reference_ids": ["ev-po-1"],
    }
    correction.update(overrides)
    return correction


@pytest.fixture()
def db(runtime_dsn, local_config, tenant_id) -> Db:
    return Db(runtime_dsn, local_config, tenant_id)


@pytest.fixture()
def write_client(runtime_dsn, local_config):
    with _make_client(runtime_dsn, local_config, writes=True) as client:
        yield client


@pytest.fixture()
def api(write_client, tenant_id, seeded_case) -> Api:
    return Api(write_client, tenant_id, seeded_case)


# ------------------------------------------------------------
# Typed contract + capabilities (task §5/§6)
# ------------------------------------------------------------


def test_openapi_exposes_typed_command_and_capability_payloads(runtime_dsn, local_config):
    with _make_client(runtime_dsn, local_config, writes=False) as client:
        schema = client.get("/openapi.json").json()

    schemas = schema["components"]["schemas"]
    for name in ("ValidationOnlyCommandResponse", "CommandResultResponse", "WorkflowResumeResponse",
                 "ApiErrorEnvelope", "CommandCapabilitiesResponse"):
        assert name in schemas, name

    post = schema["paths"]["/api/v1/review-cases/{review_case_id}/commands"]["post"]
    envelope_ref = post["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" in envelope_ref
    envelope = schemas[envelope_ref["$ref"].rsplit("/", 1)[1]]
    data_schema = envelope["properties"]["data"]
    assert "anyOf" in data_schema and any("anyOf" in part or "$ref" in part for part in data_schema["anyOf"])
    for status_code in ("401", "403", "404", "409", "422", "500", "503"):
        assert status_code in post["responses"], status_code

    assert "EXECUTE_PAYMENT" not in schemas["ReviewAction"]["enum"]
    assert not any("PAY" in value for value in schemas["ReviewAction"]["enum"])
    assert "/api/v1/review-cases/{review_case_id}/command-capabilities" in schema["paths"]


def test_capabilities_for_reviewer_expose_only_advisory_data(api, tenant_id, seeded_case):
    caps = api.caps()

    assert caps["command_mode"] == "COMMIT"
    assert caps["assignment"] == "UNASSIGNED"
    assert caps["case_status"] == "OPEN"
    assert caps["review_revision"] == 1 and caps["workflow_revision"] == 1
    assert caps["permitted_actions"] == ["CLAIM", "RELEASE", "ACCEPT", "CORRECT", "REJECT", "RESUME_WORKFLOW"]
    assert [item["action"] for item in caps["available_actions"]] == ["CLAIM"]
    assert caps["unsupported_actions"] == [
        "CONFIRM_SUPPLIER", "CONFIRM_PURCHASE_ORDER", "REQUEST_INFORMATION", "ESCALATE"
    ]
    assert caps["payment_execution"] == "PROHIBITED"

    policy = caps["correction_policy"]
    assert "ev-po-1" in policy["evidence_reference_ids"]
    assert policy["line_numbers"] == [1]
    assert policy["require_reason"] is True and policy["require_evidence"] is True
    headers = {item["field_name"]: item["current_value"] for item in policy["header_fields"]}
    # M11E.4: every explicitly configured header field is offered; one never extracted has current_value null.
    from ap_agent.models.interface import default_interface_config

    assert set(headers) == {field.value for field in default_interface_config().correctable_header_fields}
    assert headers["PURCHASE_ORDER_NUMBER"] == "99" and headers["SUPPLIER_NAME"] is None
    assert [item["current_value"] for item in policy["header_fields"] if item["field_name"] not in {"PURCHASE_ORDER_NUMBER"}] == [None] * (len(headers) - 1)
    assert {"line_number": 1, "field_name": "LINE_QUANTITY", "current_value": "5"} in policy["line_values"]

    response_text = api.client.get(
        f"/api/v1/review-cases/{seeded_case.review_case_id}/command-capabilities",
        headers=_headers(tenant_id, REVIEWER, InterfaceRole.AP_REVIEWER),
    ).text
    assert str(tenant_id) not in response_text
    assert REVIEWER not in response_text


def test_capabilities_reflect_role(api, tenant_id):
    auditor = api.as_actor("auditor-1", InterfaceRole.READ_ONLY_AUDITOR).caps()
    assert auditor["permitted_actions"] == [] and auditor["available_actions"] == []

    operator = api.as_actor("operator-1", InterfaceRole.AP_OPERATOR).caps()
    assert operator["permitted_actions"] == ["CLAIM", "RELEASE", "CORRECT"]
    assert [item["action"] for item in operator["available_actions"]] == ["CLAIM"]


def test_capabilities_unknown_and_cross_tenant_case_return_404(runtime_dsn, local_config, write_client, seeded_case):
    other_tenant = uuid.uuid4()
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    PostgresMemoryRepository(runtime_dsn, local_config).register_tenant(
        tenant_id=other_tenant, tenant_key=f"m11c-other-{other_tenant.hex[:8]}", display_name="Other"
    )

    for tenant, case_id in ((other_tenant, seeded_case.review_case_id), (other_tenant, uuid.uuid4())):
        response = write_client.get(
            f"/api/v1/review-cases/{case_id}/command-capabilities",
            headers=_headers(tenant, REVIEWER, InterfaceRole.AP_REVIEWER),
        )
        assert response.status_code == 404


# ------------------------------------------------------------
# A. Claim and release
# ------------------------------------------------------------


def test_a_claim_then_release_updates_state_and_appends_audit(api, db, seeded_case):
    workflow_id = seeded_case.workflow_id
    assert db.audit_count(workflow_id, "INTERFACE_COMMAND_EXECUTED") == 0

    claimed = api.run("CLAIM", key="m11c-a-claim-0001")
    assert claimed["status"] == "ACCEPTED" and claimed["data"]["resulting_case_status"] == "IN_REVIEW"
    assert db.case(seeded_case.review_case_id)[:2] == ("CLAIMED", REVIEWER)
    assert db.lock_version(workflow_id) == 2

    caps = api.caps()
    assert caps["assignment"] == "ASSIGNED_TO_ACTOR" and caps["workflow_revision"] == 2
    assert {item["action"] for item in caps["available_actions"]} == {"RELEASE", "ACCEPT", "CORRECT", "REJECT"}

    released = api.run("RELEASE", key="m11c-a-release-001")
    assert released["data"]["resulting_case_status"] == "OPEN"
    assert db.case(seeded_case.review_case_id)[:2] == ("OPEN", None)
    assert db.lock_version(workflow_id) == 3
    assert db.audit_count(workflow_id, "INTERFACE_COMMAND_EXECUTED") == 2


# ------------------------------------------------------------
# B. Approve and resume
# ------------------------------------------------------------


def test_b_approve_then_resume_creates_a_memory_persistence_handoff(api, db, seeded_case):
    workflow_id, case_id = seeded_case.workflow_id, seeded_case.review_case_id
    memory_before = db.memory(workflow_id)

    api.run("CLAIM", key="m11c-b-claim-0001")
    approved = api.run("ACCEPT", key="m11c-b-accept-001")
    assert approved["status"] == "ACCEPTED"
    assert approved["data"]["resulting_case_status"] == "RESOLVED"
    assert approved["data"]["workflow_resumed"] is False

    decisions = db.decisions(case_id)
    assert len(decisions) == 1 and decisions[0][1] == "APPROVED" and decisions[0][2] == REVIEWER
    assert db.case(case_id)[0] == "RESOLVED"

    caps = api.caps()
    assert caps["case_status"] == "RESOLVED"
    assert caps["resume"] == {
        "eligible": True, "already_requested": False, "disposition": "APPROVED",
        "decision_id": str(decisions[0][0]), "ineligible_reason": None,
    }
    assert [item["action"] for item in caps["available_actions"]] == ["RESUME_WORKFLOW"]

    workflow_revision_before_resume = db.lock_version(workflow_id)
    resumed = api.resume(key="m11c-b-resume-001", disposition="APPROVED")
    data = resumed["data"]
    assert resumed["status"] == "ACCEPTED" and data["workflow_resumed"] is True
    assert data["restart_stage"] == "MEMORY_PERSISTENCE"
    assert data["derived_version"].startswith("human-review-derived-")
    assert data["resume_plan_id"]
    assert db.lock_version(workflow_id) == workflow_revision_before_resume + 1
    assert db.audit_count(workflow_id, "WORKFLOW_RESUME_REQUESTED") == 1
    assert db.memory(workflow_id) == memory_before

    after = api.caps()
    assert after["resume"]["already_requested"] is True and after["resume"]["eligible"] is False
    assert after["resume"]["ineligible_reason"] == "RESUME_ALREADY_REQUESTED"

    # A second resume under a *different* key at the current revisions is a conflict (not a 500) -- never a second handoff.
    stale = api.resume(key="m11c-b-resume-002", disposition="APPROVED", expect=409)
    assert "WORKFLOW_NOT_AWAITING_RESUME" in stale["errors"]
    assert db.audit_count(workflow_id, "WORKFLOW_RESUME_REQUESTED") == 1

    # ...and a stale observed revision is still a plain stale-revision conflict.
    really_stale = api.post(api.body("RESUME_WORKFLOW", key="m11c-b-resume-003", disposition="APPROVED",
                                     observed_workflow_revision=1))
    assert really_stale.status_code == 409 and "STALE_WORKFLOW_REVISION" in really_stale.json()["errors"]


# ------------------------------------------------------------
# C. Correct and resume (original memory immutable)
# ------------------------------------------------------------


@pytest.mark.parametrize(
    "correction,expected_stage",
    [(_line_correction(), "FINANCIAL_VALIDATION"), (_header_correction(), "REFERENCE_MATCHING")],
    ids=["financial", "reference"],
)
def test_c_correct_then_resume_keeps_original_memory_immutable(api, db, seeded_case, correction, expected_stage):
    workflow_id, case_id = seeded_case.workflow_id, seeded_case.review_case_id
    memory_before = db.memory(workflow_id)

    api.run("CLAIM", key="m11c-c-claim-0001")
    corrected = api.run("CORRECT", key="m11c-c-correct-01", corrections=[correction])
    assert corrected["data"]["resulting_case_status"] == "RESOLVED"

    decisions = db.decisions(case_id)
    assert len(decisions) == 1 and decisions[0][1] == "CORRECTED"
    stored = decisions[0][3]["corrections"]
    assert stored[0]["field_name"] == correction["field_name"]
    assert stored[0]["evidence_reference_ids"] == ["ev-po-1"]
    assert stored[0]["reason"] == correction["reason"]

    # Original normalized invoice memory (and its payload hash) untouched.
    assert db.memory(workflow_id) == memory_before

    resumed = api.resume(key="m11c-c-resume-001", disposition="CORRECTED")
    assert resumed["data"]["restart_stage"] == expected_stage
    assert db.memory(workflow_id) == memory_before


def test_c_multiple_corrections_in_one_command(api, db, seeded_case):
    api.run("CLAIM", key="m11c-c2-claim-001")
    api.run("CORRECT", key="m11c-c2-correct-1", corrections=[_line_correction(), _header_correction()])
    assert len(db.decisions(seeded_case.review_case_id)[0][3]["corrections"]) == 2
    resumed = api.resume(key="m11c-c2-resume-01", disposition="CORRECTED")
    assert resumed["data"]["restart_stage"] == "FINANCIAL_VALIDATION"


# ------------------------------------------------------------
# D. Reject
# ------------------------------------------------------------


def test_d_reject_is_terminal_and_not_resumable(api, db, seeded_case):
    case_id = seeded_case.review_case_id
    api.run("CLAIM", key="m11c-d-claim-0001")
    rejected = api.run("REJECT", key="m11c-d-reject-001", reason_codes=["NOT_OUR_INVOICE"], notes="Wrong supplier.")
    assert rejected["data"]["resulting_case_status"] == "REJECTED"

    decisions = db.decisions(case_id)
    assert len(decisions) == 1 and decisions[0][1] == "REJECTED"
    assert db.audit_count(seeded_case.workflow_id, "INTERFACE_COMMAND_EXECUTED") == 2

    caps = api.caps()
    assert caps["case_status"] == "REJECTED"
    assert caps["resume"]["eligible"] is False
    assert caps["resume"]["ineligible_reason"] == "RESOLVED_CASE_REQUIRED"
    assert caps["available_actions"] == []

    blocked = api.resume(key="m11c-d-resume-001", disposition="APPROVED", expect=422)
    assert "RESOLVED_CASE_REQUIRED" in blocked["errors"]
    assert db.audit_count(seeded_case.workflow_id, "WORKFLOW_RESUME_REQUESTED") == 0


def test_d_reject_without_notes_or_reasons_is_rejected(api, db, seeded_case):
    api.run("CLAIM", key="m11c-d2-claim-001")
    before = db.total_state()
    for overrides, code in (
        ({"notes": None}, "NOTES_REQUIRED"),
        ({"notes": "   "}, "NOTES_REQUIRED"),
        ({"reason_codes": []}, "REASON_CODE_REQUIRED"),
    ):
        body = api.body("REJECT", key="m11c-d2-reject-01", **overrides)
        response = api.post(body)
        assert response.status_code == 422 and code in response.json()["errors"]
    assert db.total_state() == before


# ------------------------------------------------------------
# E. Genuinely concurrent claim
# ------------------------------------------------------------


def test_e_concurrent_claim_has_exactly_one_winner(runtime_dsn, local_config, tenant_id, seeded_case, db):
    contenders = 4
    barrier = threading.Barrier(contenders)
    results: dict[str, Any] = {}

    def attempt(index: int) -> None:
        actor = f"m11c-racer-{index}"
        with _make_client(runtime_dsn, local_config, writes=True) as client:
            body = {
                "command_id": str(uuid.uuid4()),
                "idempotency_key": f"m11c-e-claim-{index:04d}",
                "action": "CLAIM",
                "disposition": None,
                "observed_review_revision": 1,
                "observed_workflow_revision": 1,
                "reason_codes": [],
                "notes": None,
                "corrections": [],
                "requested_at": interface_utc_now().isoformat(),
            }
            headers = _headers(tenant_id, actor, InterfaceRole.AP_REVIEWER)
            barrier.wait(timeout=30)
            response = client.post(
                f"/api/v1/review-cases/{seeded_case.review_case_id}/commands", headers=headers, json=body
            )
            results[actor] = response

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(contenders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(results) == contenders
    winners = [actor for actor, response in results.items() if response.status_code == 200]
    losers = [response for response in results.values() if response.status_code != 200]
    assert len(winners) == 1
    assert all(response.status_code == 409 for response in losers)
    assert all(set(response.json()["errors"]) & {"STALE_WORKFLOW_REVISION", "CASE_NOT_OPEN"} for response in losers)

    status, assigned_to, _ = db.case(seeded_case.review_case_id)
    assert status == "CLAIMED" and assigned_to == winners[0]
    assert db.audit_count(seeded_case.workflow_id, "INTERFACE_COMMAND_EXECUTED") == 1
    assert db.lock_version(seeded_case.workflow_id) == 2


def test_e_claim_after_another_reviewer_claimed_is_a_409_conflict(api, db, seeded_case):
    api.run("CLAIM", key="m11c-e2-claim-001")
    other = api.as_actor(OTHER_REVIEWER)
    response = other.post(other.body("CLAIM", key="m11c-e2-claim-002"))
    assert response.status_code == 409
    assert {"CASE_NOT_OPEN", "CASE_ALREADY_ASSIGNED"} <= set(response.json()["errors"])
    assert db.case(seeded_case.review_case_id)[1] == REVIEWER


# ------------------------------------------------------------
# F. Idempotency
# ------------------------------------------------------------


def test_f_identical_retry_is_idempotent_and_writes_nothing_new(api, db, seeded_case):
    body = api.body("CLAIM", key="m11c-f-claim-0001")
    first = api.post(body)
    assert first.status_code == 200 and first.json()["status"] == "ACCEPTED"
    state = db.total_state()

    retry = api.post(body)
    assert retry.status_code == 200 and retry.json()["status"] == "IDEMPOTENT"
    assert retry.json()["data"]["resulting_case_status"] == "IN_REVIEW"
    assert db.total_state() == state

    approve = api.body("ACCEPT", key="m11c-f-accept-001")
    first_decision = api.post(approve)
    assert first_decision.json()["status"] == "ACCEPTED"
    decision_state = db.total_state()
    retried_decision = api.post(approve)
    assert retried_decision.status_code == 200 and retried_decision.json()["status"] == "IDEMPOTENT"
    assert retried_decision.json()["data"]["decision_id"] == first_decision.json()["data"]["decision_id"]
    assert db.total_state() == decision_state
    assert len(db.decisions(seeded_case.review_case_id)) == 1

    resume = api.body("RESUME_WORKFLOW", key="m11c-f-resume-001", disposition="APPROVED")
    first_resume = api.post(resume)
    assert first_resume.status_code == 200
    resume_state = db.total_state()
    retried_resume = api.post(resume)
    assert retried_resume.status_code == 200 and retried_resume.json()["status"] == "IDEMPOTENT"
    assert retried_resume.json()["data"]["restart_stage"] == first_resume.json()["data"]["restart_stage"]
    assert db.total_state() == resume_state


def test_f_reused_key_with_different_content_is_a_409(api, db):
    api.run("CLAIM", key="m11c-f2-claim-001")
    state = db.total_state()

    conflicting = api.body("RELEASE", key="m11c-f2-claim-001")
    response = api.post(conflicting)
    assert response.status_code == 409
    assert "IDEMPOTENCY_KEY_CONTENT_CONFLICT" in response.json()["errors"]
    assert db.total_state() == state


# ------------------------------------------------------------
# G. Stale revision
# ------------------------------------------------------------


@pytest.mark.parametrize("field,expected", [
    ("observed_review_revision", "STALE_REVIEW_REVISION"),
    ("observed_workflow_revision", "STALE_WORKFLOW_REVISION"),
])
def test_g_stale_revisions_return_409_and_mutate_nothing(api, db, field, expected):
    before = db.total_state()
    response = api.post(api.body("CLAIM", key=f"m11c-g-{field[9:16]}-001", **{field: 42}))
    assert response.status_code == 409 and expected in response.json()["errors"]
    assert db.total_state() == before


def test_g_stale_decision_after_release_by_someone_else(api, db, seeded_case):
    api.run("CLAIM", key="m11c-g2-claim-001")
    stale_caps = api.caps()
    api.run("RELEASE", key="m11c-g2-release-01")
    before = db.total_state()
    response = api.post(api.body("ACCEPT", key="m11c-g2-accept-001", caps=stale_caps))
    assert response.status_code == 409
    assert db.total_state() == before


# ------------------------------------------------------------
# H. Authorization + tenancy
# ------------------------------------------------------------


def test_h_auditor_and_operator_role_limits(api, db):
    before = db.total_state()
    auditor = api.as_actor("auditor-1", InterfaceRole.READ_ONLY_AUDITOR)
    response = auditor.post(auditor.body("CLAIM", key="m11c-h-auditor-001"))
    assert response.status_code == 403 and "ACTION_NOT_PERMITTED" in response.json()["errors"]

    operator = api.as_actor("operator-1", InterfaceRole.AP_OPERATOR)
    operator.run("CLAIM", key="m11c-h-operator-c1")
    approve = operator.post(operator.body("ACCEPT", key="m11c-h-operator-a1"))
    assert approve.status_code == 403
    assert db.total_state() != before  # the operator's own legitimate claim only
    assert len(db.decisions(api.case.review_case_id)) == 0


def test_h_other_reviewer_cannot_release_or_decide_a_claimed_case(api, db, seeded_case):
    api.run("CLAIM", key="m11c-h2-claim-001")
    state = db.total_state()
    other = api.as_actor(OTHER_REVIEWER)

    for action, key in (("RELEASE", "m11c-h2-release-01"), ("ACCEPT", "m11c-h2-accept-001"),
                        ("REJECT", "m11c-h2-reject-001")):
        response = other.post(other.body(action, key=key, caps=api.caps()))
        assert response.status_code == 409, action
        assert "CASE_ASSIGNED_TO_DIFFERENT_REVIEWER" in response.json()["errors"]

    assert db.total_state() == state


def test_h_only_the_deciding_reviewer_may_resume(api, db, seeded_case):
    api.run("CLAIM", key="m11c-h3-claim-001")
    api.run("ACCEPT", key="m11c-h3-accept-001")
    other = api.as_actor(OTHER_REVIEWER)
    assert other.caps()["resume"]["ineligible_reason"] == "DECISION_ACTOR_MISMATCH"
    response = other.post(other.body("RESUME_WORKFLOW", key="m11c-h3-resume-001", disposition="APPROVED"))
    assert response.status_code == 409 and "DECISION_ACTOR_MISMATCH" in response.json()["errors"]
    assert db.audit_count(seeded_case.workflow_id, "WORKFLOW_RESUME_REQUESTED") == 0


def test_h_cross_tenant_command_fails_closed_and_mutates_nothing(runtime_dsn, local_config, api, db, seeded_case):
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    other_tenant = uuid.uuid4()
    PostgresMemoryRepository(runtime_dsn, local_config).register_tenant(
        tenant_id=other_tenant, tenant_key=f"m11c-x-{other_tenant.hex[:8]}", display_name="Other tenant"
    )
    before = db.total_state()

    body = api.body("CLAIM", key="m11c-h4-claim-001")
    response = api.client.post(
        f"/api/v1/review-cases/{seeded_case.review_case_id}/commands",
        headers=_headers(other_tenant, "intruder", InterfaceRole.AP_REVIEWER),
        json=body,
    )
    assert response.status_code == 404
    assert db.total_state() == before


def test_h_interleaved_tenants_never_see_each_others_cases(runtime_dsn, local_config, api, seeded_case):
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository
    from tests.support.review_fixtures import seed_review_case

    other_tenant = uuid.uuid4()
    PostgresMemoryRepository(runtime_dsn, local_config).register_tenant(
        tenant_id=other_tenant, tenant_key=f"m11c-y-{other_tenant.hex[:8]}", display_name="Other tenant"
    )
    other_case = seed_review_case(runtime_dsn, local_config, tenant_id=other_tenant)

    for _ in range(6):
        mine = api.client.get("/api/v1/review-cases", headers=_headers(api.tenant_id, REVIEWER, InterfaceRole.AP_REVIEWER))
        theirs = api.client.get("/api/v1/review-cases", headers=_headers(other_tenant, "x", InterfaceRole.AP_REVIEWER))
        assert [item["review_case_id"] for item in mine.json()["data"]["items"]] == [str(seeded_case.review_case_id)]
        assert [item["review_case_id"] for item in theirs.json()["data"]["items"]] == [str(other_case.review_case_id)]


def test_h_command_on_one_case_never_touches_another_case(runtime_dsn, local_config, api, db, seeded_case):
    from tests.support.review_fixtures import seed_review_case

    sibling = seed_review_case(runtime_dsn, local_config, tenant_id=api.tenant_id, source_name="sibling.pdf")
    sibling_before = (db.case(sibling.review_case_id), db.lock_version(sibling.workflow_id),
                      db.decisions(sibling.review_case_id), db.memory(sibling.workflow_id))

    api.run("CLAIM", key="m11c-h5-claim-001")
    api.run("ACCEPT", key="m11c-h5-accept-001")

    assert (db.case(sibling.review_case_id), db.lock_version(sibling.workflow_id),
            db.decisions(sibling.review_case_id), db.memory(sibling.workflow_id)) == sibling_before


# ------------------------------------------------------------
# I. Invalid corrections
# ------------------------------------------------------------


@pytest.mark.parametrize(
    "correction,expected_status,expected_code",
    [
        (_header_correction(reason="   "), 422, "CORRECTION_REASON_REQUIRED"),
        (_header_correction(reason=""), 422, "REQUEST_VALIDATION_FAILED"),
        (_header_correction(evidence_reference_ids=[]), 422, "CORRECTION_EVIDENCE_REQUIRED"),
        (_header_correction(evidence_reference_ids=["ev-not-real"]), 422, "UNKNOWN_EVIDENCE_REFERENCE"),
        (_header_correction(field_name="ACCOUNT_NUMBER"), 422, "REQUEST_VALIDATION_FAILED"),
        (_line_correction(line_number=99), 422, "UNKNOWN_INVOICE_LINE_NUMBER"),
        (_line_correction(line_number=None), 422, "LINE_CORRECTION_LINE_NUMBER_REQUIRED"),
        (_header_correction(line_number=1), 422, "HEADER_CORRECTION_LINE_NUMBER_PROHIBITED"),
        (_header_correction(previous_value="not-the-stored-value"), 422, "PREVIOUS_VALUE_MISMATCH"),
        (_header_correction(corrected_value="99"), 422, "CORRECTED_VALUE_UNCHANGED"),
        # M11E.4: a missing header may be inserted only with previous_value null; a forged previous value is refused
        (_header_correction(field_name="INVOICE_NUMBER", previous_value="x"), 422, "PREVIOUS_VALUE_MISMATCH"),
    ],
)
def test_i_invalid_corrections_are_rejected_without_mutation(api, db, correction, expected_status, expected_code):
    api.run("CLAIM", key="m11c-i-claim-0001")
    before = db.total_state()

    response = api.post(api.body("CORRECT", key="m11c-i-correct-001", corrections=[correction]))

    assert response.status_code == expected_status
    assert expected_code in response.json()["errors"]
    assert db.total_state() == before


def test_i_validation_errors_never_echo_input(api):
    api.run("CLAIM", key="m11c-i2-claim-001")
    secret = "sk-live-super-secret-value"
    response = api.post(api.body("CORRECT", key="m11c-i2-correct-01",
                                 corrections=[_header_correction(field_name=secret)]))
    assert response.status_code == 422
    assert secret not in response.text
    assert set(response.json()) == {"request_id", "errors", "generated_at"}


# ------------------------------------------------------------
# J. Payment / ERP / bank prohibition
# ------------------------------------------------------------


@pytest.mark.parametrize("action", ["EXECUTE_PAYMENT", "RELEASE_PAYMENT", "BANK_TRANSFER", "POST_TO_ERP", "PAY"])
def test_j_payment_like_actions_are_rejected_before_execution(api, db, action):
    before = db.total_state()
    response = api.post(api.body("CLAIM", key="m11c-j-payment-001", action=action))
    assert response.status_code == 422
    assert "REVIEW_ACTION_NOT_SUPPORTED" in response.json()["errors"]
    assert db.total_state() == before


def test_j_payment_fields_are_rejected_as_unknown_properties(api, db):
    before = db.total_state()
    for extra in ({"payment_amount": "100.00"}, {"bank_account": "GB00"}, {"erp_posting": True},
                  {"tenant_id": str(uuid.uuid4())}, {"actor_id": "spoof"}, {"actor_role": "TENANT_ADMIN"}):
        body = api.body("CLAIM", key="m11c-j-extra-0001")
        body.update(extra)
        response = api.post(body)
        assert response.status_code == 422, extra
    assert db.total_state() == before


# ------------------------------------------------------------
# Validation-only mode
# ------------------------------------------------------------


def test_validation_only_mode_never_mutates(runtime_dsn, local_config, tenant_id, seeded_case, db):
    with _make_client(runtime_dsn, local_config, writes=False) as client:
        api = Api(client, tenant_id, seeded_case)
        caps = api.caps()
        assert caps["command_mode"] == "VALIDATION_ONLY"

        before = db.total_state()
        response = api.post(api.body("CLAIM", key="m11c-v-claim-0001"))
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "VALIDATED"
        assert set(body["data"]) == {"command_id", "review_case_id", "action", "command_fingerprint",
                                     "execution_mode", "database_mutation"}
        assert body["data"]["execution_mode"] == "VALIDATION_ONLY" and body["data"]["database_mutation"] is False
        assert db.total_state() == before
        assert db.case(seeded_case.review_case_id)[0] == "OPEN"

        resume = api.post(api.body("RESUME_WORKFLOW", key="m11c-v-resume-0001", disposition="APPROVED"))
        assert resume.status_code == 422
        assert "RESUME_REQUIRES_RESOLVED_DECISION" in resume.json()["errors"]
        assert db.total_state() == before


# ------------------------------------------------------------
# Wire shape of committed results
# ------------------------------------------------------------


def test_committed_result_json_shapes(api):
    claim = api.run("CLAIM", key="m11c-w-claim-0001")
    assert set(claim["data"]) == {"command_id", "idempotency_key", "status", "review_case_id", "document_id",
                                  "resulting_case_status", "resulting_revision", "workflow_resumed",
                                  "decision_id", "message"}
    api.run("ACCEPT", key="m11c-w-accept-0001")
    resume = api.resume(key="m11c-w-resume-0001", disposition="APPROVED")
    assert {"restart_stage", "derived_version", "resume_plan_id"} <= set(resume["data"])


# ------------------------------------------------------------
# Rollback on failure
# ------------------------------------------------------------


def test_failure_mid_transaction_rolls_everything_back(monkeypatch, runtime_dsn, local_config, tenant_id, seeded_case, db):
    from ap_agent.repositories.review_repository import ReviewRepository

    with _make_client(runtime_dsn, local_config, writes=True) as client:
        api = Api(client, tenant_id, seeded_case)
        api.run("CLAIM", key="m11c-r-claim-0001")
        before = db.total_state()

        def explode(*args, **kwargs):
            raise RuntimeError("simulated failure after the decision insert")

        monkeypatch.setattr(ReviewRepository, "resolve_review_case", explode)

    with _make_client(runtime_dsn, local_config, writes=True, raise_server_exceptions=False) as client:
        failing = Api(client, tenant_id, seeded_case)
        response = failing.post(failing.body("ACCEPT", key="m11c-r-accept-0001"))
        assert response.status_code == 500
        assert response.json()["errors"] == ["INTERNAL_ERROR"]
        assert "simulated" not in response.text

    assert db.total_state() == before
    assert db.decisions(seeded_case.review_case_id) == []
    assert db.case(seeded_case.review_case_id)[0] == "CLAIMED"

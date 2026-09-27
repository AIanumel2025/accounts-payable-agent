"""M10 `requires_postgres` acceptance suite: `ap_agent.repositories.review_repository`
and `ap_agent.services.review_commands`/`review_decisions`/`workflow_resume`
against the real, isolated `ap_agent_m8_test` database (task §17).

Same DSN-sourcing safety gate as
`tests/integration/test_memory_postgres_integration.py` (see that
module's docstring for the full rationale): reads only
`AP_AGENT_TEST_POSTGRES_DSN`, verifies its `dbname` before connecting, and
drives every RLS-sensitive test through a dedicated least-privilege
runtime role, never the DSN's own migration-owner role.
"""

from __future__ import annotations

import os
import secrets
import threading
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest

from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy
from ap_agent.models.interface import (
    HumanReviewDisposition,
    InterfaceActor,
    InterfaceCommandStatus,
    InterfaceRole,
    ReviewAction,
    ReviewCaseStatus,
    ReviewCommand,
    ReviewFieldCorrection,
    default_interface_config,
    interface_utc_now,
)
from ap_agent.repositories.review_repository import ReviewRepository
from ap_agent.services.review_commands import execute_assignment_command
from ap_agent.services.review_decisions import execute_decision_command
from ap_agent.services.workflow_resume import execute_resume_command

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

TEST_DSN_ENV_VAR = "AP_AGENT_TEST_POSTGRES_DSN"
EXPECTED_TEST_DATABASE = "ap_agent_m8_test"
RUNTIME_ROLE_NAME = "ap_agent_m10_test_runtime"


def _owner_dsn_or_skip() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR, "").strip()

    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not configured.")

    from psycopg.conninfo import conninfo_to_dict

    dbname = conninfo_to_dict(dsn).get("dbname")

    if dbname != EXPECTED_TEST_DATABASE:
        pytest.fail(
            f"{TEST_DSN_ENV_VAR} targets database {dbname!r}, expected {EXPECTED_TEST_DATABASE!r}. "
            "Refusing to run (fail-closed)."
        )

    return dsn


@pytest.fixture(scope="session")
def owner_dsn() -> str:
    return _owner_dsn_or_skip()


@pytest.fixture(scope="session")
def local_config() -> MemoryConfig:
    return MemoryConfig(transport_policy=PostgresTransportPolicy(require_ssl=True, require_channel_binding=True))


@pytest.fixture(scope="session")
def applied_migrations(owner_dsn, local_config):
    from ap_agent.db.migration_runner import apply_all_migrations

    return apply_all_migrations(owner_dsn, local_config)


@pytest.fixture(scope="session")
def runtime_role_ready(owner_dsn, local_config, applied_migrations) -> dict[str, str]:
    from ap_agent.db import roles as db_roles

    password = secrets.token_urlsafe(32)
    db_roles.create_least_privilege_role(owner_dsn, local_config, role_name=RUNTIME_ROLE_NAME, password=password)
    db_roles.grant_schema_access(owner_dsn, local_config, role_name=RUNTIME_ROLE_NAME)

    flags = db_roles.role_privilege_flags(owner_dsn, local_config, role_name=RUNTIME_ROLE_NAME)
    assert flags["superuser"] is False
    assert flags["bypassrls"] is False

    return {"role_name": RUNTIME_ROLE_NAME, "password": password}


@pytest.fixture(scope="session")
def runtime_dsn(owner_dsn, runtime_role_ready) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(owner_dsn)
    params["user"] = runtime_role_ready["role_name"]
    params["password"] = runtime_role_ready["password"]

    return make_conninfo(**params)


@pytest.fixture()
def repository(runtime_dsn, local_config) -> ReviewRepository:
    return ReviewRepository(runtime_dsn, local_config)


@pytest.fixture()
def tenant_id(runtime_dsn, local_config) -> uuid.UUID:
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    new_tenant_id = uuid.uuid4()
    PostgresMemoryRepository(runtime_dsn, local_config).register_tenant(
        tenant_id=new_tenant_id, tenant_key=f"m10-repo-{new_tenant_id.hex[:8]}", display_name="M10 Repository Test Tenant"
    )
    return new_tenant_id


@pytest.fixture()
def config():
    return default_interface_config()


def _seed(runtime_dsn, local_config, tenant_id, **kwargs):
    from tests.support.review_fixtures import seed_review_case

    return seed_review_case(runtime_dsn, local_config, tenant_id=tenant_id, **kwargs)


def _actor(tenant_id, role: InterfaceRole, actor_id: str) -> InterfaceActor:
    return InterfaceActor(actor_id=actor_id, tenant_id=tenant_id, role=role, authenticated_at=interface_utc_now())


def _claim_command(seeded, actor, *, idempotency_key: str, observed_review_revision=1, observed_workflow_revision=1) -> ReviewCommand:
    return ReviewCommand(
        command_id=uuid.uuid4(),
        idempotency_key=idempotency_key,
        tenant_id=seeded.tenant_id,
        workflow_id=seeded.workflow_id,
        batch_id=seeded.batch_id,
        document_id=seeded.document_id,
        review_case_id=seeded.review_case_id,
        actor=actor,
        action=ReviewAction.CLAIM,
        disposition=None,
        observed_review_revision=observed_review_revision,
        observed_workflow_revision=observed_workflow_revision,
        reason_codes=tuple(),
        notes=None,
        corrections=tuple(),
        requested_at=interface_utc_now(),
    )


# ==============================================================
# 1. Review-queue / dashboard / detail retrieval
# ==============================================================


def test_review_queue_retrieval(runtime_dsn, local_config, repository, tenant_id):
    seeded = _seed(runtime_dsn, local_config, tenant_id)

    records, total_count = repository.list_review_queue(tenant_id)

    assert total_count == 1
    assert records[0].review_case_id == seeded.review_case_id
    assert records[0].revision == 1


def test_dashboard_aggregation(runtime_dsn, local_config, repository, tenant_id):
    _seed(runtime_dsn, local_config, tenant_id)

    dashboard, source_rows = repository.get_dashboard(tenant_id)

    assert dashboard.total_invoices == 1
    assert dashboard.review_required_invoices == 1
    assert dashboard.open_review_cases == 1
    assert dashboard.unassigned_review_cases == 1
    assert len(source_rows) == 1


def test_invoice_detail_retrieval_and_payload_hash_verification(runtime_dsn, local_config, repository, tenant_id):
    seeded = _seed(runtime_dsn, local_config, tenant_id)
    queue_record = repository.get_review_queue_record(tenant_id, seeded.review_case_id)

    detail = repository.get_invoice_detail(tenant_id, queue_record)

    assert detail.document_id == seeded.document_id
    assert len(detail.fields) == 2
    assert len(detail.financial_checks) == 1
    assert len(detail.line_matches) == 1
    assert detail.source_artifact_uri is None


# ==============================================================
# 2. Claim / release / correction / decision
# ==============================================================


def test_claim_then_release(runtime_dsn, local_config, repository, tenant_id, config):
    seeded = _seed(runtime_dsn, local_config, tenant_id)
    actor = _actor(tenant_id, InterfaceRole.AP_OPERATOR, "operator-1")

    claim_result = execute_assignment_command(
        repository, _claim_command(seeded, actor, idempotency_key="repo-claim-1"), config
    )

    assert claim_result.status == InterfaceCommandStatus.ACCEPTED
    assert claim_result.resulting_case_status == ReviewCaseStatus.IN_REVIEW

    release_command = ReviewCommand(
        command_id=uuid.uuid4(), idempotency_key="repo-release-1", tenant_id=seeded.tenant_id,
        workflow_id=seeded.workflow_id, batch_id=seeded.batch_id, document_id=seeded.document_id,
        review_case_id=seeded.review_case_id, actor=actor, action=ReviewAction.RELEASE, disposition=None,
        observed_review_revision=1, observed_workflow_revision=2, reason_codes=tuple(), notes=None,
        corrections=tuple(), requested_at=interface_utc_now(),
    )

    release_result = execute_assignment_command(repository, release_command, config)

    assert release_result.status == InterfaceCommandStatus.ACCEPTED
    assert release_result.resulting_case_status == ReviewCaseStatus.OPEN


def test_idempotent_retry_returns_the_same_result(runtime_dsn, local_config, repository, tenant_id, config):
    seeded = _seed(runtime_dsn, local_config, tenant_id)
    actor = _actor(tenant_id, InterfaceRole.AP_OPERATOR, "operator-1")
    command = _claim_command(seeded, actor, idempotency_key="repo-idempotent-1")

    first = execute_assignment_command(repository, command, config)
    second = execute_assignment_command(repository, command, config)

    assert first.status == InterfaceCommandStatus.ACCEPTED
    assert second.status == InterfaceCommandStatus.IDEMPOTENT
    assert second.resulting_case_status == first.resulting_case_status


def test_conflicting_idempotency_key_reuse_is_a_conflict(runtime_dsn, local_config, repository, tenant_id, config):
    seeded = _seed(runtime_dsn, local_config, tenant_id)
    actor = _actor(tenant_id, InterfaceRole.AP_OPERATOR, "operator-1")
    first_command = _claim_command(seeded, actor, idempotency_key="repo-conflict-1")

    execute_assignment_command(repository, first_command, config)

    conflicting_command = replace(first_command, command_id=uuid.uuid4(), notes="different content, same key")
    result = execute_assignment_command(repository, conflicting_command, config)

    assert result.status == InterfaceCommandStatus.CONFLICT
    assert "IDEMPOTENCY_KEY_CONTENT_CONFLICT" in result.errors


def test_stale_revision_is_rejected(runtime_dsn, local_config, repository, tenant_id, config):
    seeded = _seed(runtime_dsn, local_config, tenant_id)
    actor = _actor(tenant_id, InterfaceRole.AP_OPERATOR, "operator-1")
    command = _claim_command(seeded, actor, idempotency_key="repo-stale-1", observed_review_revision=99)

    result = execute_assignment_command(repository, command, config)

    assert result.status == InterfaceCommandStatus.REJECTED
    assert "STALE_REVIEW_REVISION" in result.errors


def test_evidence_backed_correction_and_append_only_decision(runtime_dsn, local_config, repository, tenant_id, config):
    from ap_agent.models.normalization import InvoiceFieldName

    seeded = _seed(runtime_dsn, local_config, tenant_id)
    reviewer = _actor(tenant_id, InterfaceRole.AP_REVIEWER, "reviewer-1")

    claim_result = execute_assignment_command(
        repository,
        _claim_command(seeded, reviewer, idempotency_key="repo-correction-claim-1"),
        config,
    )
    assert claim_result.status == InterfaceCommandStatus.ACCEPTED

    correction = ReviewFieldCorrection(
        field_name=InvoiceFieldName.PURCHASE_ORDER_NUMBER,
        line_number=None, previous_value="99", corrected_value="99A",
        reason="Reviewer verified the PO against evidence.", evidence_reference_ids=("ev-po-1",),
    )
    correction_command = ReviewCommand(
        command_id=uuid.uuid4(), idempotency_key="repo-correction-1", tenant_id=seeded.tenant_id,
        workflow_id=seeded.workflow_id, batch_id=seeded.batch_id, document_id=seeded.document_id,
        review_case_id=seeded.review_case_id, actor=reviewer, action=ReviewAction.CORRECT,
        disposition=HumanReviewDisposition.CORRECTED, observed_review_revision=1, observed_workflow_revision=2,
        reason_codes=("PURCHASE_ORDER_CORRECTED",), notes="PO reviewed.", corrections=(correction,),
        requested_at=interface_utc_now(),
    )

    decision_result = execute_decision_command(repository, correction_command, config)

    assert decision_result.status == InterfaceCommandStatus.ACCEPTED
    assert decision_result.resulting_case_status == ReviewCaseStatus.RESOLVED
    assert decision_result.decision_id is not None


def test_correction_without_evidence_is_rejected(runtime_dsn, local_config, repository, tenant_id, config):
    from ap_agent.models.normalization import InvoiceFieldName

    seeded = _seed(runtime_dsn, local_config, tenant_id)
    reviewer = _actor(tenant_id, InterfaceRole.AP_REVIEWER, "reviewer-1")

    execute_assignment_command(repository, _claim_command(seeded, reviewer, idempotency_key="repo-noevidence-claim-1"), config)

    correction = ReviewFieldCorrection(
        field_name=InvoiceFieldName.PURCHASE_ORDER_NUMBER, line_number=None, previous_value="99",
        corrected_value="99A", reason="Reviewer verified.", evidence_reference_ids=(),
    )
    command = ReviewCommand(
        command_id=uuid.uuid4(), idempotency_key="repo-noevidence-1", tenant_id=seeded.tenant_id,
        workflow_id=seeded.workflow_id, batch_id=seeded.batch_id, document_id=seeded.document_id,
        review_case_id=seeded.review_case_id, actor=reviewer, action=ReviewAction.CORRECT,
        disposition=HumanReviewDisposition.CORRECTED, observed_review_revision=1, observed_workflow_revision=2,
        reason_codes=("PURCHASE_ORDER_CORRECTED",), notes="PO reviewed.", corrections=(correction,),
        requested_at=interface_utc_now(),
    )

    result = execute_decision_command(repository, command, config)

    assert result.status == InterfaceCommandStatus.REJECTED
    assert "CORRECTION_EVIDENCE_REQUIRED" in result.errors


def test_auditor_mutation_is_rejected(runtime_dsn, local_config, repository, tenant_id, config):
    seeded = _seed(runtime_dsn, local_config, tenant_id)
    auditor = _actor(tenant_id, InterfaceRole.READ_ONLY_AUDITOR, "auditor-1")

    result = execute_assignment_command(repository, _claim_command(seeded, auditor, idempotency_key="repo-auditor-1"), config)

    assert result.status == InterfaceCommandStatus.REJECTED
    assert "ACTION_NOT_PERMITTED" in result.errors


def test_cross_tenant_command_is_rejected(runtime_dsn, local_config, repository, tenant_id, config):
    seeded = _seed(runtime_dsn, local_config, tenant_id)
    other_tenant_actor = _actor(uuid.uuid4(), InterfaceRole.AP_OPERATOR, "operator-1")

    result = execute_assignment_command(
        repository, _claim_command(seeded, other_tenant_actor, idempotency_key="repo-cross-tenant-1"), config
    )

    assert result.status == InterfaceCommandStatus.REJECTED
    assert "CROSS_TENANT_COMMAND" in result.errors


# ==============================================================
# 3. Workflow-resume handoff
# ==============================================================


def test_resume_handoff_persistence(runtime_dsn, local_config, repository, tenant_id, config):
    from ap_agent.models.normalization import InvoiceFieldName

    seeded = _seed(runtime_dsn, local_config, tenant_id)
    reviewer = _actor(tenant_id, InterfaceRole.AP_REVIEWER, "reviewer-1")

    execute_assignment_command(repository, _claim_command(seeded, reviewer, idempotency_key="repo-resume-claim-1"), config)

    correction = ReviewFieldCorrection(
        field_name=InvoiceFieldName.PURCHASE_ORDER_NUMBER, line_number=None, previous_value="99",
        corrected_value="99A", reason="Reviewer verified.", evidence_reference_ids=("ev-po-1",),
    )
    correction_command = ReviewCommand(
        command_id=uuid.uuid4(), idempotency_key="repo-resume-correction-1", tenant_id=seeded.tenant_id,
        workflow_id=seeded.workflow_id, batch_id=seeded.batch_id, document_id=seeded.document_id,
        review_case_id=seeded.review_case_id, actor=reviewer, action=ReviewAction.CORRECT,
        disposition=HumanReviewDisposition.CORRECTED, observed_review_revision=1, observed_workflow_revision=2,
        reason_codes=("PURCHASE_ORDER_CORRECTED",), notes="PO reviewed.", corrections=(correction,),
        requested_at=interface_utc_now(),
    )
    decision_result = execute_decision_command(repository, correction_command, config)
    assert decision_result.status == InterfaceCommandStatus.ACCEPTED

    resume_command = ReviewCommand(
        command_id=uuid.uuid4(), idempotency_key="repo-resume-1", tenant_id=seeded.tenant_id,
        workflow_id=seeded.workflow_id, batch_id=seeded.batch_id, document_id=seeded.document_id,
        review_case_id=seeded.review_case_id, actor=reviewer, action=ReviewAction.RESUME_WORKFLOW,
        disposition=HumanReviewDisposition.CORRECTED, observed_review_revision=decision_result.resulting_revision,
        observed_workflow_revision=3, reason_codes=("CORRECTION_READY_FOR_REVALIDATION",),
        notes="resume", corrections=tuple(), requested_at=interface_utc_now(),
    )

    execution = execute_resume_command(repository, resume_command, config)

    assert execution.command_result.status == InterfaceCommandStatus.ACCEPTED
    assert execution.command_result.workflow_resumed is True
    assert execution.resume_plan is not None
    from ap_agent.models.orchestration import OrchestrationStage

    assert execution.resume_plan.restart_stage == OrchestrationStage.REFERENCE_MATCHING

    retry_execution = execute_resume_command(repository, resume_command, config)
    assert retry_execution.command_result.status == InterfaceCommandStatus.IDEMPOTENT
    assert retry_execution.resume_plan == execution.resume_plan


# ==============================================================
# 4. Transaction rollback
# ==============================================================


def test_transaction_rolls_back_on_failure(runtime_dsn, local_config, repository, tenant_id, config):
    """A `FOR UPDATE`-locked review case whose workflow revision changes
    concurrently must leave no partial state -- forcing the mismatch via
    a second, independent claim first, then asserting the case is not
    left half-claimed for the first (losing) command."""

    seeded = _seed(runtime_dsn, local_config, tenant_id)
    actor_a = _actor(tenant_id, InterfaceRole.AP_OPERATOR, "operator-a")
    actor_b = _actor(tenant_id, InterfaceRole.AP_OPERATOR, "operator-b")

    winner = execute_assignment_command(repository, _claim_command(seeded, actor_a, idempotency_key="repo-rollback-a"), config)
    assert winner.status == InterfaceCommandStatus.ACCEPTED

    loser = execute_assignment_command(repository, _claim_command(seeded, actor_b, idempotency_key="repo-rollback-b"), config)

    assert loser.status == InterfaceCommandStatus.REJECTED
    assert "CASE_NOT_OPEN" in loser.errors

    context = repository.get_command_context(tenant_id, seeded.review_case_id)
    assert context.assigned_reviewer_id == "operator-a"


# ==============================================================
# 5. Cross-document / cross-tenant isolation
# ==============================================================


def test_cross_document_isolation(runtime_dsn, local_config, repository, tenant_id):
    first = _seed(runtime_dsn, local_config, tenant_id, source_name="doc-a.pdf")
    second = _seed(runtime_dsn, local_config, tenant_id, source_name="doc-b.pdf")

    records, total_count = repository.list_review_queue(tenant_id)

    assert total_count == 2
    assert {record.document_id for record in records} == {first.document_id, second.document_id}


def test_cross_tenant_isolation_with_colliding_review_semantics(runtime_dsn, local_config):
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    memory_repository = PostgresMemoryRepository(runtime_dsn, local_config)
    memory_repository.register_tenant(tenant_id=tenant_a, tenant_key=f"iso-a-{tenant_a.hex[:8]}", display_name="Iso A")
    memory_repository.register_tenant(tenant_id=tenant_b, tenant_key=f"iso-b-{tenant_b.hex[:8]}", display_name="Iso B")

    _seed(runtime_dsn, local_config, tenant_a)

    repository = ReviewRepository(runtime_dsn, local_config)
    records_a, count_a = repository.list_review_queue(tenant_a)
    records_b, count_b = repository.list_review_queue(tenant_b)

    assert count_a == 1
    assert count_b == 0
    assert records_b == tuple()


# ==============================================================
# 6. Real concurrency: two reviewers claim the same open case
# ==============================================================


def test_concurrent_claim_exactly_one_wins(runtime_dsn, local_config, tenant_id, config):
    """Two reviewers submit a CLAIM for the same open case at (as close
    to) the same instant as `threading.Barrier` can arrange. Exactly one
    must be ACCEPTED; the other must receive a controlled REJECTED
    (`CASE_NOT_OPEN`/`CASE_ALREADY_ASSIGNED`), never a second ACCEPTED and
    never an unhandled exception (task §17: "Add a real concurrency test
    in which two reviewers attempt to claim the same open case
    simultaneously.")."""

    seeded = _seed(runtime_dsn, local_config, tenant_id)

    repository_a = ReviewRepository(runtime_dsn, local_config)
    repository_b = ReviewRepository(runtime_dsn, local_config)

    actor_a = _actor(tenant_id, InterfaceRole.AP_REVIEWER, "reviewer-a")
    actor_b = _actor(tenant_id, InterfaceRole.AP_REVIEWER, "reviewer-b")

    command_a = _claim_command(seeded, actor_a, idempotency_key="repo-concurrent-a")
    command_b = _claim_command(seeded, actor_b, idempotency_key="repo-concurrent-b")

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def _run(name, repository, command):
        try:
            barrier.wait(timeout=10)
            results[name] = execute_assignment_command(repository, command, config)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [
        threading.Thread(target=_run, args=("a", repository_a, command_a)),
        threading.Thread(target=_run, args=("b", repository_b, command_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2

    statuses = sorted(result.status for result in results.values())
    assert statuses == sorted([InterfaceCommandStatus.ACCEPTED, InterfaceCommandStatus.REJECTED])

    rejected_result = next(r for r in results.values() if r.status == InterfaceCommandStatus.REJECTED)
    assert {"CASE_NOT_OPEN", "CASE_ALREADY_ASSIGNED"} & set(rejected_result.errors)

    final_context = ReviewRepository(runtime_dsn, local_config).get_command_context(tenant_id, seeded.review_case_id)
    assert final_context.case_status == ReviewCaseStatus.IN_REVIEW
    assert final_context.assigned_reviewer_id in {"reviewer-a", "reviewer-b"}


# ==============================================================
# 7. Connection-pool reuse (no tenant-context leakage)
# ==============================================================


def test_connection_pool_reuse_without_tenant_context_leakage(runtime_dsn, local_config):
    from ap_agent.db.connection import create_connection_pool, set_tenant_context

    tenant_a = uuid.uuid4()
    pool = create_connection_pool(runtime_dsn, local_config)
    pool.open(wait=True, timeout=30)

    try:
        with pool.connection() as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_a))
                cursor.execute("SELECT current_setting('ap_agent.tenant_id', TRUE);")
                assert cursor.fetchone()[0] == str(tenant_a)

        with pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('ap_agent.tenant_id', TRUE);")
                assert cursor.fetchone()[0] in (None, "")
    finally:
        pool.close()

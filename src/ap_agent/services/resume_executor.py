"""`RESUME_WORKFLOW` job execution (M11D Core).

Runs only inside the worker. M11C's resume command persisted a *plan* (in
its `WORKFLOW_RESUME_REQUESTED` audit payload) and enqueued this job; this
executor consumes that plan:

  1. loads the stored decision, audit plan and effective memory (the
     original record, or the derived version a second review cycle reviewed);
  2. **verifies every stored hash and identity before any stage runs**
     (payload hash, normalization hash, correction-overlay hash, plan and
     derived-version identity, decision evidence, source-document hash and --
     when the originating upload artifact exists -- its bytes);
  3. hydrates the stored phase payloads into their typed models, applies the
     correction overlay to a deep copy, and hands the explicitly hydrated
     upstream results to `resume_invoice_workflow`, which runs *only* the
     plan's restart stage and later stages (never ingestion, preprocessing,
     OCR or normalization);
  4. appends the effective result as a new `invoice_memory_versions` row
     (never touching `invoice_memory_records`), opens a **new** review case
     if downstream checks still require review, settles the workflow and the
     job, and writes an audit event -- all in one transaction.

A failed verification or stage fails the job closed; the workflow is never
marked successful on failure.
"""

from __future__ import annotations

import dataclasses
import logging
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional
from uuid import NAMESPACE_URL, UUID, uuid5

from ap_agent.artifacts.storage import ArtifactStore
from ap_agent.config.postgres import MemoryConfig
from ap_agent.exceptions import ArtifactIntegrityError, ResumeIntegrityError
from ap_agent.models.matching import ReferenceDataBundle
from ap_agent.models.memory import HumanReviewDisposition
from ap_agent.models.operations import WorkflowJob, WorkflowJobStatus
from ap_agent.models.orchestration import (
    InvoiceWorkflowRequest,
    InvoiceWorkflowResult,
    InvoiceWorkflowStatus,
    MemoryPersistenceStageResult,
    OrchestrationFailureClass,
    OrchestrationStage,
    default_orchestration_config,
)
from ap_agent.orchestration.engine import (
    StageHandlerRegistry,
    WorkflowExecutionContext,
    blocking_retry_waiter,
    resume_invoice_workflow,
)
from ap_agent.orchestration.handlers import build_stage_handler_registry
from ap_agent.repositories.operations_repository import (
    DerivedVersionRecord,
    OperationsRepository,
    derived_review_id,
    derived_version_id,
    insert_derived_version,
    open_derived_review_case,
)
from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository
from ap_agent.serialization.hydration import (
    HydrationError,
    hydrate_financial_validation_result,
    hydrate_matching_result,
    hydrate_normalization_result,
)
from ap_agent.serialization.memory_json import (
    canonical_json_bytes,
    canonical_payload_sha256,
    enum_text,
    memory_json_safe,
    normalized_field_value,
    optional_decimal,
    optional_text,
)
from ap_agent.services.correction_overlay import apply_correction_overlay, corrected_field_names, parse_overlay
from ap_agent.services.job_events import build_job_event_sink, safe_error_code
from ap_agent.services.memory_service import MemoryService, build_matched_reference_snapshot
from ap_agent.services.workflow_resume import choose_review_restart_stage
from ap_agent.tools.matching import build_matching_input
from ap_agent.tools.normalization import persist_normalization_result
from ap_agent.models.normalization import InvoiceFieldName
from ap_agent.worker.pipeline import PipelineConfigs, build_pipeline_configs, build_production_registry

__all__ = ["ResumeJobExecutor", "VerifiedResume", "verify_resume_inputs"]

_LOGGER = logging.getLogger("ap_agent.worker")

_RESUME_STAGES = (
    OrchestrationStage.FINANCIAL_VALIDATION,
    OrchestrationStage.REFERENCE_MATCHING,
    OrchestrationStage.MEMORY_PERSISTENCE,
)
_UPSTREAM_OF_RESTART = (
    OrchestrationStage.INGESTION,
    OrchestrationStage.PREPROCESSING,
    OrchestrationStage.OCR,
    OrchestrationStage.NORMALIZATION,
)


@dataclasses.dataclass(frozen=True)
class VerifiedResume:
    """Inputs that passed every integrity check (nothing here is trusted
    before `verify_resume_inputs` returns it)."""

    job: WorkflowJob
    plan: dict[str, Any]
    restart_stage: OrchestrationStage
    decision_id: UUID
    resume_plan_id: UUID
    overlay: dict[str, Any]
    workflow_id: UUID
    batch_id: UUID
    document_id: UUID
    source_document_sha256: str
    original_memory_record_id: UUID
    source_memory_version_id: Optional[UUID]
    base_payload_sha256: str
    base_normalized: dict[str, Any]
    base_financial: dict[str, Any]
    base_matching: dict[str, Any]
    base_reference: dict[str, Any]
    existing_version_id: Optional[UUID]


def _fail(code: str) -> ResumeIntegrityError:
    return ResumeIntegrityError(code)


def _payload_hash(normalized: Any, financial: Any, matching: Any, reference: Any) -> str:
    return canonical_payload_sha256(
        {
            "normalization_result": normalized,
            "financial_validation_result": financial,
            "matching_result": matching,
            "matched_reference_data": reference,
        }
    )


def verify_resume_inputs(
    cursor: Any,
    job: WorkflowJob,
    *,
    artifact_store: Optional[ArtifactStore] = None,
) -> VerifiedResume:
    """Load and verify everything the resume depends on, inside one
    read-only tenant-scoped transaction. Raises `ResumeIntegrityError`
    (a stable content-free code) on any difference."""

    tenant_id = job.tenant_id

    if job.resume_plan_id is None or job.workflow_id is None or job.review_id is None:
        raise _fail("RESUME_JOB_INCOMPLETE")

    cursor.execute(
        """
        SELECT payload FROM ap_agent.audit_events
        WHERE tenant_id = %s AND workflow_id = %s AND event_type = 'WORKFLOW_RESUME_REQUESTED'
          AND payload->>'resume_plan_id' = %s;
        """,
        (tenant_id, job.workflow_id, str(job.resume_plan_id)),
    )
    audit_rows = cursor.fetchall()

    if len(audit_rows) != 1 or not isinstance(audit_rows[0][0], dict):
        raise _fail("RESUME_PLAN_NOT_FOUND")

    plan = audit_rows[0][0]

    try:
        decision_id = UUID(plan["decision_id"])
        restart_stage = OrchestrationStage(plan["restart_stage"])
        disposition = HumanReviewDisposition(plan["disposition"])
        overlay_json = plan["correction_overlay_json"]
        overlay_sha = plan["correction_overlay_sha256"]
        source_normalization_sha = plan["source_normalization_sha256"]
        source_payload_sha = plan["source_memory_payload_sha256"]
        derived_version = plan["derived_version"]
        original_record_id = UUID(plan["source_memory_record_id"])
        plan_version_id = None if plan.get("source_memory_version_id") is None else UUID(plan["source_memory_version_id"])
    except (KeyError, ValueError, TypeError) as error:
        raise _fail("RESUME_PLAN_MALFORMED") from error

    if restart_stage not in _RESUME_STAGES:
        raise _fail("RESUME_RESTART_STAGE_INVALID")

    if disposition not in {HumanReviewDisposition.APPROVED, HumanReviewDisposition.CORRECTED}:
        raise _fail("RESUME_DISPOSITION_INVALID")

    # -- plan identity ---------------------------------------------------
    if sha256(str(overlay_json).encode("utf-8")).hexdigest() != overlay_sha:
        raise _fail("RESUME_OVERLAY_HASH_MISMATCH")

    if derived_version != f"human-review-derived-{overlay_sha[:16]}":
        raise _fail("RESUME_DERIVED_VERSION_MISMATCH")

    if job.resume_plan_id != uuid5(decision_id, f"phase-9-resume-plan:{restart_stage.value}:{overlay_sha}"):
        raise _fail("RESUME_PLAN_IDENTITY_MISMATCH")

    overlay = parse_overlay(overlay_json)

    # -- decision, review case, workflow --------------------------------
    cursor.execute(
        """
        SELECT decision_id, review_id, workflow_id, decision_type, evidence
        FROM ap_agent.review_decisions WHERE tenant_id = %s AND decision_id = %s;
        """,
        (tenant_id, decision_id),
    )
    decision = cursor.fetchone()

    if decision is None:
        raise _fail("RESUME_DECISION_NOT_FOUND")

    _, decision_review_id, decision_workflow_id, decision_type, evidence = decision

    if decision_review_id != job.review_id or decision_workflow_id != job.workflow_id:
        raise _fail("RESUME_IDENTITY_MISMATCH")

    if decision_type != disposition.value or not isinstance(evidence, dict):
        raise _fail("RESUME_DECISION_MISMATCH")

    if canonical_json_bytes(evidence.get("corrections", [])) != canonical_json_bytes(overlay["corrections"]):
        raise _fail("RESUME_OVERLAY_TAMPERED")

    if str(overlay.get("decision_id")) != str(decision_id):
        raise _fail("RESUME_OVERLAY_TAMPERED")

    try:
        corrected_fields = tuple(InvoiceFieldName(name) for name in corrected_field_names(overlay))
        expected_restart = choose_review_restart_stage(disposition, corrected_fields)
    except ValueError as error:
        raise _fail("RESUME_RESTART_STAGE_MISMATCH") from error

    if expected_restart != restart_stage:
        raise _fail("RESUME_RESTART_STAGE_MISMATCH")

    cursor.execute(
        """
        SELECT document_id, batch_id, source_document_sha256
        FROM ap_agent.workflow_instances WHERE tenant_id = %s AND workflow_id = %s;
        """,
        (tenant_id, job.workflow_id),
    )
    workflow = cursor.fetchone()

    if workflow is None:
        raise _fail("RESUME_WORKFLOW_NOT_FOUND")

    document_id, batch_id, workflow_source_sha = workflow
    workflow_source_sha = workflow_source_sha.strip()

    if (job.document_id is not None and job.document_id != document_id) or (
        job.batch_id is not None and job.batch_id != batch_id
    ):
        raise _fail("RESUME_IDENTITY_MISMATCH")

    # -- original memory (must be unchanged) -----------------------------
    cursor.execute(
        """
        SELECT payload_sha256, normalized_invoice, financial_validation, matching_result,
               matched_reference_data, source_document_sha256, workflow_id, document_id
        FROM ap_agent.invoice_memory_records WHERE tenant_id = %s AND memory_record_id = %s;
        """,
        (tenant_id, original_record_id),
    )
    original = cursor.fetchone()

    if original is None or original[6] != job.workflow_id or original[7] != document_id:
        raise _fail("RESUME_ORIGINAL_MEMORY_NOT_FOUND")

    if _payload_hash(original[1], original[2], original[3], original[4]) != original[0].strip():
        raise _fail("RESUME_ORIGINAL_MEMORY_HASH_MISMATCH")

    if original[5].strip() != workflow_source_sha:
        raise _fail("RESUME_SOURCE_DOCUMENT_HASH_MISMATCH")

    # -- effective memory the decision was made against ------------------
    cursor.execute(
        """
        SELECT original_memory_record_id, memory_version_id, payload_sha256,
               normalized_invoice, financial_validation, matching_result, matched_reference_data
        FROM ap_agent.review_case_effective_memory WHERE tenant_id = %s AND review_id = %s;
        """,
        (tenant_id, job.review_id),
    )
    effective = cursor.fetchone()

    if effective is None:
        raise _fail("RESUME_EFFECTIVE_MEMORY_NOT_FOUND")

    effective_record_id, effective_version_id, effective_sha, base_norm, base_fin, base_match, base_ref = effective

    if effective_record_id != original_record_id or effective_version_id != plan_version_id:
        raise _fail("RESUME_SOURCE_MEMORY_MISMATCH")

    base_hash = _payload_hash(base_norm, base_fin, base_match, base_ref)

    if base_hash != effective_sha.strip() or base_hash != source_payload_sha:
        raise _fail("RESUME_SOURCE_MEMORY_HASH_MISMATCH")

    if sha256(canonical_json_bytes(base_norm)).hexdigest() != source_normalization_sha:
        raise _fail("RESUME_NORMALIZATION_HASH_MISMATCH")

    if str(overlay.get("source_normalization_sha256")) != source_normalization_sha:
        raise _fail("RESUME_OVERLAY_TAMPERED")

    if not isinstance(base_norm, dict) or base_norm.get("source_document_sha256") != workflow_source_sha:
        raise _fail("RESUME_SOURCE_DOCUMENT_HASH_MISMATCH")

    # -- originating upload artifact, where one exists -------------------
    if artifact_store is not None:
        cursor.execute(
            """
            SELECT artifact_uri, artifact_sha256 FROM ap_agent.workflow_jobs
            WHERE tenant_id = %s AND workflow_id = %s AND job_type = 'PROCESS_DOCUMENT' LIMIT 1;
            """,
            (tenant_id, job.workflow_id),
        )
        origin = cursor.fetchone()

        if origin is not None:
            if origin[1].strip() != workflow_source_sha:
                raise _fail("RESUME_ARTIFACT_HASH_MISMATCH")

            try:
                artifact_store.verify_integrity(origin[0], tenant_id=tenant_id, expected_sha256=origin[1])
            except ArtifactIntegrityError as error:
                raise _fail("RESUME_ARTIFACT_INTEGRITY") from error

    cursor.execute(
        "SELECT version_id FROM ap_agent.invoice_memory_versions WHERE tenant_id = %s AND resume_plan_id = %s;",
        (tenant_id, job.resume_plan_id),
    )
    existing = cursor.fetchone()

    return VerifiedResume(
        job=job, plan=plan, restart_stage=restart_stage, decision_id=decision_id,
        resume_plan_id=job.resume_plan_id, overlay=overlay, workflow_id=job.workflow_id, batch_id=batch_id,
        document_id=document_id, source_document_sha256=workflow_source_sha,
        original_memory_record_id=original_record_id, source_memory_version_id=plan_version_id,
        base_payload_sha256=base_hash, base_normalized=base_norm, base_financial=base_fin,
        base_matching=base_match, base_reference=base_ref,
        existing_version_id=None if existing is None else existing[0],
    )


def _forbidden_stage(stage: OrchestrationStage) -> Callable[[WorkflowExecutionContext], Any]:
    def handler(_context: WorkflowExecutionContext) -> Any:
        raise RuntimeError(f"{stage.value} must not run during a workflow resume.")

    return handler


class ResumeJobExecutor:
    def __init__(
        self,
        *,
        dsn: str,
        memory_config: MemoryConfig,
        operations: OperationsRepository,
        reference_data: ReferenceDataBundle,
        phase_directory_for: Callable[[WorkflowJob], Path],
        ocr_provider_name: str = "paddleocr",
        artifact_store: Optional[ArtifactStore] = None,
    ) -> None:
        self._dsn = dsn
        self._memory_config = memory_config
        self._operations = operations
        self._reference_data = reference_data
        self._phase_directory_for = phase_directory_for
        self._ocr_provider_name = ocr_provider_name
        self._artifact_store = artifact_store

    # -- helpers -----------------------------------------------------------

    def _fail_job(self, job: WorkflowJob, code: str, message: str, *, stage: Optional[str] = None,
                  summary: Optional[dict[str, Any]] = None) -> WorkflowJob:
        with self._operations.transaction(job.tenant_id) as cursor:
            return self._operations.finish_job(
                cursor, job, status=WorkflowJobStatus.FAILED, event_type="JOB_FAILED", message=message,
                error_code=safe_error_code(code), summary=summary, stage=stage,
            )

    def _build_registry(
        self, job: WorkflowJob, configs: PipelineConfigs, verified: VerifiedResume
    ) -> StageHandlerRegistry:
        memory_service = MemoryService(
            PostgresMemoryRepository(self._dsn, self._memory_config), tenant_id=job.tenant_id
        )
        _, bindings = build_production_registry(
            configs=configs, reference_data=self._reference_data, ocr_engine=None, ocr_engine_version="not-used",
            memory_service=memory_service,
        )
        handlers: dict[OrchestrationStage, Callable[[WorkflowExecutionContext], Any]] = {
            stage: _forbidden_stage(stage) for stage in _UPSTREAM_OF_RESTART
        }
        handlers[OrchestrationStage.FINANCIAL_VALIDATION] = bindings.run_financial_validation
        handlers[OrchestrationStage.REFERENCE_MATCHING] = bindings.run_reference_matching
        handlers[OrchestrationStage.MEMORY_PERSISTENCE] = self._memory_handler(verified)

        return build_stage_handler_registry(handlers)

    def _memory_handler(self, verified: VerifiedResume) -> Callable[[WorkflowExecutionContext], Any]:
        """The resume analogue of `ProductionPhaseBindings.run_memory_persistence`:
        decides the final review state. The row itself is appended by
        `_persist_outcome` in the same transaction as the workflow/job
        settlement."""

        version_id = derived_version_id(verified.job.tenant_id, verified.resume_plan_id)

        def handler(context: WorkflowExecutionContext) -> MemoryPersistenceStageResult:
            normalization = context.result_for(OrchestrationStage.NORMALIZATION)
            financial = context.result_for(OrchestrationStage.FINANCIAL_VALIDATION)
            matching = context.result_for(OrchestrationStage.REFERENCE_MATCHING)

            if not (normalization.document_id == financial.document_id == matching.document_id == verified.document_id):
                raise RuntimeError("Resumed phase results belong to different documents.")

            if verified.restart_stage == OrchestrationStage.MEMORY_PERSISTENCE:
                # An approval resolves the old review; nothing is recomputed.
                review_required = False
                reasons: tuple[str, ...] = tuple()
            else:
                review_required = bool(context.review_required or matching.review_required)
                reasons = tuple(enum_text(reason) for reason in matching.review_reasons)

                if review_required and not reasons:
                    reasons = ("INHERITED_REVIEW_REQUIRED",)

            return MemoryPersistenceStageResult(
                batch_id=verified.batch_id,
                document_id=verified.document_id,
                status="REVIEW_REQUIRED" if review_required else "SUCCEEDED",
                review_required=review_required,
                review_reasons=reasons,
                memory_record_id=version_id,
                payload_sha256=verified.base_payload_sha256,
                stored_record=None,
            )

        return handler

    # -- execution ---------------------------------------------------------

    def execute(self, job: WorkflowJob) -> WorkflowJob:
        try:
            with self._operations.transaction(job.tenant_id) as cursor:
                cursor.execute("SET TRANSACTION READ ONLY;")
                verified = verify_resume_inputs(cursor, job, artifact_store=self._artifact_store)
        except ResumeIntegrityError as error:
            return self._fail_job(job, error.code, "Stored resume inputs failed integrity verification.")

        self._operations.append_event(
            tenant_id=job.tenant_id, job_id=job.job_id, event_type="RESUME_INPUTS_VERIFIED", status="RUNNING",
            stage=verified.restart_stage.value, attempt_number=1,
            message="Stored hashes and identities verified before execution.",
        )

        try:
            upstream, corrected_normalization = self._hydrate_upstream(verified, job)
        except (HydrationError, ResumeIntegrityError) as error:
            code = error.code if isinstance(error, ResumeIntegrityError) else "RESUME_PAYLOAD_HYDRATION_FAILED"
            return self._fail_job(job, code, "Stored phase payloads could not be rebuilt for the resume.")

        configs = build_pipeline_configs(self._phase_directory_for(job), ocr_provider=self._ocr_provider_name)

        if corrected_normalization is not None:
            # The Phase 5 bridge verifies a persisted Phase 4 artifact for the
            # normalization it is given; materialise it for the *corrected*
            # result with the existing Phase 4 persistence function.
            persist_normalization_result(
                normalization_input=SimpleNamespace(
                    batch_id=corrected_normalization.batch_id, document_id=corrected_normalization.document_id
                ),
                result=corrected_normalization,
                config=configs.normalization,
            )

        request = InvoiceWorkflowRequest(
            tenant_id=job.tenant_id,
            batch_id=verified.batch_id,
            correlation_id=uuid5(NAMESPACE_URL, f"ap-agent/job-correlation/{job.tenant_id}/{job.job_id}"),
            source_path=Path(job.source_name),
            source_channel="WORKFLOW_RESUME",
            submitted_at=job.created_at,
            resume_document_id=verified.document_id,
        )

        result = resume_invoice_workflow(
            request=request,
            handler_registry=self._build_registry(job, configs, verified),
            config=default_orchestration_config(),
            restart_stage=verified.restart_stage,
            upstream_results=upstream,
            retry_waiter=blocking_retry_waiter,
            event_sink=build_job_event_sink(self._operations, job),
        )

        return self._settle(job, verified, result)

    def _hydrate_upstream(
        self, verified: VerifiedResume, job: WorkflowJob
    ) -> tuple[dict[OrchestrationStage, Any], Any]:
        normalization = hydrate_normalization_result(verified.base_normalized)
        financial = hydrate_financial_validation_result(verified.base_financial)

        if verified.restart_stage == OrchestrationStage.MEMORY_PERSISTENCE:
            matching = hydrate_matching_result(verified.base_matching)

            return (
                {
                    OrchestrationStage.NORMALIZATION: normalization,
                    OrchestrationStage.FINANCIAL_VALIDATION: financial,
                    OrchestrationStage.REFERENCE_MATCHING: matching,
                },
                None,
            )

        corrected = apply_correction_overlay(normalization, verified.overlay, decision_id=verified.decision_id)

        if verified.restart_stage == OrchestrationStage.FINANCIAL_VALIDATION:
            return {OrchestrationStage.NORMALIZATION: corrected}, corrected

        # The earlier financial review is resolved by the human decision; its
        # checks remain as evidence, only the inherited review flags clear.
        resolved_financial = dataclasses.replace(financial, review_required=False, review_reasons=tuple())

        return (
            {
                OrchestrationStage.NORMALIZATION: corrected,
                OrchestrationStage.FINANCIAL_VALIDATION: resolved_financial,
            },
            corrected,
        )

    # -- settlement --------------------------------------------------------

    def _build_version_record(
        self, verified: VerifiedResume, result: InvoiceWorkflowResult
    ) -> DerivedVersionRecord:
        job = verified.job
        normalization = result.normalization_result
        financial = result.financial_validation_result
        matching = result.matching_result
        memory_stage = result.memory_bundle

        assert normalization is not None and financial is not None and matching is not None and memory_stage is not None

        if verified.restart_stage == OrchestrationStage.MEMORY_PERSISTENCE:
            normalized_payload = verified.base_normalized
            financial_payload = verified.base_financial
            matching_payload = verified.base_matching
            reference_payload = verified.base_reference
        else:
            normalized_payload = memory_json_safe(normalization)
            matching_payload = memory_json_safe(matching)
            matching_input = build_matching_input(normalization, financial, self._reference_data)
            reference_payload = build_matched_reference_snapshot(matching_input, matching)
            financial_payload = (
                memory_json_safe(financial)
                if verified.restart_stage == OrchestrationStage.FINANCIAL_VALIDATION
                else verified.base_financial
            )

        record = normalization.invoice_record
        assert record is not None

        review_required = bool(memory_stage.review_required)
        reasons = tuple(memory_stage.review_reasons)

        if review_required and not reasons:
            reasons = ("INHERITED_REVIEW_REQUIRED",)

        tenant_id = job.tenant_id
        supplier_status = enum_text(matching.supplier_resolution.status)

        return DerivedVersionRecord(
            version_id=derived_version_id(tenant_id, verified.resume_plan_id),
            tenant_id=tenant_id,
            workflow_id=verified.workflow_id,
            batch_id=verified.batch_id,
            document_id=verified.document_id,
            parent_memory_record_id=verified.original_memory_record_id,
            parent_memory_version_id=verified.source_memory_version_id,
            resume_plan_id=verified.resume_plan_id,
            decision_id=verified.decision_id,
            derived_version=str(verified.plan["derived_version"]),
            restart_stage=verified.restart_stage.value,
            source_document_sha256=verified.source_document_sha256,
            source_memory_payload_sha256=verified.base_payload_sha256,
            source_normalization_sha256=str(verified.plan["source_normalization_sha256"]),
            correction_overlay_sha256=str(verified.plan["correction_overlay_sha256"]),
            invoice_number=optional_text(normalized_field_value(record, "INVOICE_NUMBER")),
            supplier_name=optional_text(normalized_field_value(record, "SUPPLIER_NAME")),
            currency=optional_text(normalized_field_value(record, "CURRENCY")),
            total_amount=optional_decimal(normalized_field_value(record, "TOTAL_AMOUNT")),
            normalization_status=enum_text(normalization.status),
            financial_validation_status=enum_text(financial.status),
            matching_status=enum_text(matching.status),
            supplier_resolution_status=supplier_status,
            purchase_order_status=enum_text(matching.purchase_order_resolution.status),
            review_required=review_required,
            review_reasons=reasons,
            terminal_status="REVIEW_REQUIRED" if review_required else "SUCCEEDED",
            new_review_id=derived_review_id(tenant_id, verified.resume_plan_id) if review_required else None,
            normalized_invoice=normalized_payload,
            financial_validation=financial_payload,
            matching_result=matching_payload,
            matched_reference_data=reference_payload,
            payload_sha256=_payload_hash(normalized_payload, financial_payload, matching_payload, reference_payload),
        )

    def _append_audit_event(self, cursor: Any, verified: VerifiedResume, payload: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        job = verified.job
        cursor.execute(
            """
            SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM ap_agent.audit_events
            WHERE tenant_id = %s AND workflow_id = %s;
            """,
            (job.tenant_id, verified.workflow_id),
        )
        sequence_number = int(cursor.fetchone()[0])

        cursor.execute(
            """
            INSERT INTO ap_agent.audit_events
                (event_id, tenant_id, workflow_id, sequence_number, event_type, phase_name, event_status,
                 actor_type, actor_id, message, payload, occurred_at)
            VALUES (%s, %s, %s, %s, 'WORKFLOW_RESUME_EXECUTED', 'HUMAN_REVIEW', %s, 'SYSTEM',
                    'ap-agent-worker', %s, %s, transaction_timestamp())
            ON CONFLICT (event_id) DO NOTHING;
            """,
            (
                uuid5(NAMESPACE_URL, f"ap-agent/resume-executed/{job.tenant_id}/{verified.resume_plan_id}"),
                job.tenant_id, verified.workflow_id, sequence_number,
                "REVIEW_REQUIRED" if payload["review_required"] else "SUCCEEDED",
                "Workflow resume executed from the recorded restart stage.", Jsonb(payload),
            ),
        )

    def _settle(self, job: WorkflowJob, verified: VerifiedResume, result: InvoiceWorkflowResult) -> WorkflowJob:
        executed_stages = tuple(record.stage.value for record in result.stage_records)

        base_summary: dict[str, Any] = {
            "restart_stage": verified.restart_stage.value,
            "derived_version": str(verified.plan["derived_version"]),
            "decision_id": str(verified.decision_id),
            "resume_plan_id": str(verified.resume_plan_id),
            "executed_stages": list(executed_stages),
            "corrected_fields": list(corrected_field_names(verified.overlay)),
            "stages": [
                {"stage": record.stage.value, "status": record.status.value, "attempt": record.attempt_number}
                for record in result.stage_records
            ],
        }

        if result.status not in {InvoiceWorkflowStatus.SUCCEEDED, InvoiceWorkflowStatus.REVIEW_REQUIRED}:
            failed = next((record for record in reversed(result.stage_records) if record.failure_class), None)
            stage_name = failed.stage.value if failed is not None else result.current_stage.value
            failure_class = (
                failed.failure_class.value
                if failed is not None and isinstance(failed.failure_class, OrchestrationFailureClass)
                else "UNKNOWN"
            )
            return self._fail_job(
                job, f"{stage_name}_{failure_class}_FAILURE", f"Resume stopped at {stage_name}.", stage=stage_name,
                summary={**base_summary, "outcome": "FAILED", "workflow_status": result.status.value},
            )

        record = self._build_version_record(verified, result)
        review_required = record.review_required

        with self._operations.transaction(job.tenant_id) as cursor:
            created = insert_derived_version(cursor, record)

            if review_required:
                open_derived_review_case(
                    cursor, tenant_id=job.tenant_id, workflow_id=verified.workflow_id,
                    review_id=record.new_review_id, version_id=record.version_id,  # type: ignore[arg-type]
                    resume_plan_id=verified.resume_plan_id, reason_codes=record.review_reasons,
                    summary=f"Review required after resume ({record.derived_version}).",
                )

            self._operations.finalize_workflow_state(
                cursor, tenant_id=job.tenant_id, workflow_id=verified.workflow_id, review_required=review_required
            )

            self._append_audit_event(
                cursor, verified,
                {
                    "job_id": str(job.job_id),
                    "resume_plan_id": str(verified.resume_plan_id),
                    "decision_id": str(verified.decision_id),
                    "restart_stage": verified.restart_stage.value,
                    "derived_version": record.derived_version,
                    "memory_version_id": str(record.version_id),
                    "version_created": bool(created),
                    "review_required": review_required,
                    "new_review_case_id": None if record.new_review_id is None else str(record.new_review_id),
                    "original_memory_record_id": str(verified.original_memory_record_id),
                    "source_memory_payload_sha256": verified.base_payload_sha256,
                    "derived_payload_sha256": record.payload_sha256,
                    "executed_stages": list(executed_stages),
                },
            )

            summary = {
                **base_summary,
                "outcome": "RETURNED_TO_REVIEW" if review_required else "COMPLETED",
                "workflow_status": "REVIEW_REQUIRED" if review_required else "SUCCEEDED",
                "review_required": review_required,
                "review_reasons": list(record.review_reasons)[:20],
                "memory_version_id": str(record.version_id),
                "invoice_number": record.invoice_number,
                "supplier_name": record.supplier_name,
                "currency": record.currency,
                "total_amount": None if record.total_amount is None else format(record.total_amount, "f"),
                "supplier_status": record.supplier_resolution_status,
                "purchase_order_status": record.purchase_order_status,
                "financial_validation_status": record.financial_validation_status,
            }

            return self._operations.finish_job(
                cursor, job,
                status=WorkflowJobStatus.REVIEW_REQUIRED if review_required else WorkflowJobStatus.SUCCEEDED,
                event_type="JOB_REVIEW_REQUIRED" if review_required else "JOB_SUCCEEDED",
                message=(
                    "Resume finished; downstream checks still require review (a new review case was opened)."
                    if review_required
                    else "Resume finished; the workflow completed."
                ),
                summary=summary,
                result_review_id=record.new_review_id,
                result_version_id=record.version_id,
                stage=("HUMAN_REVIEW" if review_required else "COMPLETED"),
            )

"""Phase 8 production bindings: the Phase 8 engine wired to the real,
modular Phase 1-7 tools (M3 ingestion, M4 preprocessing/OCR, M5
normalisation, M6 financial validation, M7 reference matching, M8
PostgreSQL memory).

Source: notebook cell 93 ("PHASE 8 — CELL 4", "Real Phase 1-7 tool
bindings") plus corrections 4A ("Align real handlers with the Cell 3
engine-call contract"), 4B ("Resolve prior results from the engine
context"), 4C ("Isolate Phase 5 from later notebook helper collisions")
and 4D ("Isolate Phase 6 from later notebook helper collisions").

None of 4A/4B/4C/4D's actual *mechanisms* are ported, because each one
exists solely to patch a problem specific to a linear notebook sharing one
global namespace (CLAUDE.md §4), and none of those problems exist here:

  - 4A adapts real handlers with signature
    ``(request, context, attempt_number)`` to the engine's single-argument
    ``handler(context)`` call, by duck-typing arbitrary positional/keyword
    combinations. This module's handler methods are written directly
    against the engine's one-argument contract
    (`ap_agent.orchestration.engine.StageHandler`) -- there is no
    signature mismatch to adapt.
  - 4B re-derives an upstream stage's result from whatever shape the
    engine context happens to expose (a direct attribute, an accessor
    method, one of six guessed container-attribute names, a mapping or a
    tuple-of-pairs) because the notebook's Cell 4 was written before the
    real `WorkflowExecutionContext` shape was known. This module knows
    that shape precisely (it is defined two files away, in
    `ap_agent.orchestration.engine`) and calls
    `WorkflowExecutionContext.result_for(stage)` directly.
  - 4C and 4D temporarily monkeypatch module globals
    (`get_invoice_field_value`, `quantize_money`,
    `normalized_field_value`, `result_requires_review`, ...) so that
    Phase 5's and Phase 6's own helper functions are restored before each
    one runs, because a later phase cell in the *same notebook namespace*
    had reassigned those names by the time Phase 8 executes. Every
    Phase 4-7 module here (`ap_agent.tools.normalization`,
    `ap_agent.tools.financial_validation`, `ap_agent.tools.matching`,
    `ap_agent.services.memory_service`) keeps its own module-private
    helpers under Python's ordinary import scoping -- e.g.
    `ap_agent.tools.matching.normalized_field_value` (one argument) and
    `ap_agent.serialization.memory_json.normalized_field_value` (a
    different, two-argument function the memory service imports under the
    same short name) already coexist without collision, because each
    module imports its own. There is no shared global to protect.
    `tests/unit/test_orchestration_namespace_isolation.py` (task §15)
    proves this by importing all three modules together and exercising
    the PO-backed line-matching path 4D was written to protect.

4C/4D's other job -- exposing a structured Phase 5/6 ``FAILED`` result's
diagnostics instead of collapsing them into a bare `RuntimeError` -- *is*
kept, but implemented as the task brief requires (§12) rather than as the
notebook does it: `run_financial_validation`/`run_reference_matching`
below raise `ap_agent.models.orchestration.StructuredPhaseFailure`, which
carries the phase's own `errors` and `review_reasons` rather than
collapsing them into one string, and
`ap_agent.orchestration.engine.execute_invoice_workflow` classifies and
records it using that information instead of falling back to
`OrchestrationFailureClass.UNKNOWN` with an empty `errors` tuple.
"""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any, Callable, Optional
from uuid import UUID

from ap_agent.config.settings import (
    FinancialValidationConfig,
    IngestionConfig,
    MatchingConfig,
    NormalizationConfig,
    OCRConfig,
    PreprocessingConfig,
)
from ap_agent.models.matching import MatchingStatus, ReferenceDataBundle
from ap_agent.models.orchestration import (
    MemoryPersistenceStageResult,
    OrchestrationFailureClass,
    OrchestrationStage,
    StructuredPhaseFailure,
)
from ap_agent.models.validation import ValidationStatus
from ap_agent.orchestration.engine import StageHandler, StageHandlerRegistry, WorkflowExecutionContext
from ap_agent.services.memory_service import MemoryService, align_phase_results
from ap_agent.tools.financial_validation import build_validation_input, process_financial_validation
from ap_agent.tools.ingestion import ingest_document
from ap_agent.tools.matching import build_matching_input, process_invoice_matching
from ap_agent.tools.normalization import build_normalization_input, normalize_invoice_document
from ap_agent.tools.ocr import build_ocr_document_input, process_ocr_document
from ap_agent.tools.preprocessing import build_preprocessing_input, preprocess_document

__all__ = [
    "ProductionPhaseBindings",
    "build_stage_handler_registry",
]


class ProductionPhaseBindings:
    """Owns the Phase 1-7 tool configuration and the batch-scoped state
    those tools require (known content hashes for duplicate detection,
    the Phase 6 matching input each document's memory-persistence stage
    needs), and exposes one `StageHandler` per `OrchestrationStage`.

    One instance is constructed per batch (task §13: "Do not share
    mutable per-invoice context" -- the *bindings* are shared and
    thread-safe; the `WorkflowExecutionContext` each stage handler
    receives is fresh per invoice, created by
    `ap_agent.orchestration.engine.execute_invoice_workflow`). Every
    method here takes only the engine's own `WorkflowExecutionContext`,
    matching `ap_agent.orchestration.engine.StageHandler`.
    """

    def __init__(
        self,
        *,
        ingestion_config: IngestionConfig,
        preprocessing_config: PreprocessingConfig,
        ocr_config: OCRConfig,
        normalization_config: NormalizationConfig,
        financial_validation_config: FinancialValidationConfig,
        matching_config: MatchingConfig,
        reference_data: ReferenceDataBundle,
        ocr_engine: Any,
        ocr_engine_version: str,
        memory_service: MemoryService,
    ) -> None:
        self._ingestion_config = ingestion_config
        self._preprocessing_config = preprocessing_config
        self._ocr_config = ocr_config
        self._normalization_config = normalization_config
        self._financial_validation_config = financial_validation_config
        self._matching_config = matching_config
        self._reference_data = reference_data
        self._ocr_engine = ocr_engine
        self._ocr_engine_version = ocr_engine_version
        self._memory_service = memory_service

        self._known_sha256_values: set[str] = set()
        self._matching_inputs_by_document: dict[UUID, Any] = {}

        self._state_lock = RLock()

    # ----------------------------------------------------------------
    # Stage 1: Ingestion (M3)
    # ----------------------------------------------------------------

    def run_ingestion(self, context: WorkflowExecutionContext) -> Any:
        request = context.request

        # Duplicate-state access is synchronized so this bindings object
        # can supervise concurrent invoices within the same batch.
        with self._state_lock:
            result = ingest_document(
                file_path=Path(request.source_path),
                batch_id=request.batch_id,
                ingestion_source=request.source_channel,
                known_sha256_values=self._known_sha256_values,
                config=self._ingestion_config,
            )

            self._known_sha256_values.add(result.identity.sha256)

        return result

    # ----------------------------------------------------------------
    # Stage 2: Preprocessing (M4)
    # ----------------------------------------------------------------

    def run_preprocessing(self, context: WorkflowExecutionContext) -> Any:
        ingestion_result = context.result_for(OrchestrationStage.INGESTION)

        preprocessing_input = build_preprocessing_input(ingestion_result)

        return preprocess_document(preprocessing_input, self._preprocessing_config)

    # ----------------------------------------------------------------
    # Stage 3: OCR routing (M4)
    # ----------------------------------------------------------------

    def run_ocr(self, context: WorkflowExecutionContext) -> Any:
        preprocessing_result = context.result_for(OrchestrationStage.PREPROCESSING)

        ocr_input = build_ocr_document_input(
            preprocessing_result,
            self._preprocessing_config.preprocessing_version,
        )

        return process_ocr_document(
            ocr_input,
            self._ocr_config,
            engine=self._ocr_engine,
            engine_version=self._ocr_engine_version,
        )

    # ----------------------------------------------------------------
    # Stage 4: Normalisation (M5)
    # ----------------------------------------------------------------

    def run_normalization(self, context: WorkflowExecutionContext) -> Any:
        preprocessing_result = context.result_for(OrchestrationStage.PREPROCESSING)
        ocr_result = context.result_for(OrchestrationStage.OCR)

        normalization_input = build_normalization_input(
            ocr_result=ocr_result,
            preprocessing_result=preprocessing_result,
            ocr_config=self._ocr_config,
        )

        return normalize_invoice_document(normalization_input, config=self._normalization_config)

    # ----------------------------------------------------------------
    # Stage 5: Financial validation (M6)
    # ----------------------------------------------------------------

    def run_financial_validation(self, context: WorkflowExecutionContext) -> Any:
        normalization_result = context.result_for(OrchestrationStage.NORMALIZATION)

        validation_input = build_validation_input(
            normalization_result,
            normalization_config=self._normalization_config,
        )

        result = process_financial_validation(validation_input, config=self._financial_validation_config)

        # Phase 5 packages internal execution/integrity errors into a
        # structured FAILED result instead of raising (task §12): convert
        # it into a typed failure carrying its own diagnostics rather than
        # continuing to Phase 6 with a failed upstream result.
        if result.status == ValidationStatus.FAILED:
            raise StructuredPhaseFailure(
                OrchestrationStage.FINANCIAL_VALIDATION,
                errors=tuple(result.errors),
                review_reasons=tuple(result.review_reasons),
                failure_class=OrchestrationFailureClass.INTEGRITY,
            )

        return result

    # ----------------------------------------------------------------
    # Stage 6: Reference matching (M7)
    # ----------------------------------------------------------------

    def run_reference_matching(self, context: WorkflowExecutionContext) -> Any:
        normalization_result = context.result_for(OrchestrationStage.NORMALIZATION)
        financial_result = context.result_for(OrchestrationStage.FINANCIAL_VALIDATION)

        matching_input = build_matching_input(normalization_result, financial_result, self._reference_data)

        result = process_invoice_matching(matching_input, config=self._matching_config)

        # Phase 6 packages internal execution errors into a structured
        # FAILED result the same way Phase 5 does (task §12).
        if result.status == MatchingStatus.FAILED:
            raise StructuredPhaseFailure(
                OrchestrationStage.REFERENCE_MATCHING,
                errors=tuple(result.errors),
                review_reasons=tuple(result.review_reasons),
                failure_class=OrchestrationFailureClass.INTEGRITY,
            )

        with self._state_lock:
            self._matching_inputs_by_document[result.document_id] = matching_input

        return result

    # ----------------------------------------------------------------
    # Stage 7: Memory persistence (M8)
    # ----------------------------------------------------------------

    def run_memory_persistence(self, context: WorkflowExecutionContext) -> MemoryPersistenceStageResult:
        normalization_result = context.result_for(OrchestrationStage.NORMALIZATION)
        financial_result = context.result_for(OrchestrationStage.FINANCIAL_VALIDATION)
        matching_result = context.result_for(OrchestrationStage.REFERENCE_MATCHING)

        document_id = matching_result.document_id

        with self._state_lock:
            matching_input = self._matching_inputs_by_document.get(document_id)

        if matching_input is None:
            raise RuntimeError(
                f"The Phase 6 matching input is missing for document {document_id}."
            )

        if not (
            normalization_result.document_id
            == financial_result.document_id
            == matching_result.document_id
        ):
            raise RuntimeError("Phase 4-6 document identities differ.")

        if not (
            normalization_result.source_document_sha256
            == financial_result.source_document_sha256
            == matching_result.source_document_sha256
        ):
            raise RuntimeError("Phase 4-6 source hashes differ.")

        # align_phase_results (M8) re-checks the same identity/hash
        # continuity generically for any number of documents (never a
        # fixed fixture count -- CLAUDE.md decisions D-2/D-3) and raises
        # ap_agent.exceptions.MemoryIntegrityError, not a bare assertion,
        # on failure.
        (aligned,) = align_phase_results(
            normalization_results=(normalization_result,),
            matching_inputs=(matching_input,),
            matching_results=(matching_result,),
        )

        stored_record = self._memory_service.persist_document(aligned)

        review_required = bool(stored_record.review_required)
        final_status = "REVIEW_REQUIRED" if review_required else "SUCCEEDED"

        return MemoryPersistenceStageResult(
            batch_id=stored_record.batch_id,
            document_id=stored_record.document_id,
            status=final_status,
            review_required=review_required,
            review_reasons=tuple(stored_record.review_reasons),
            memory_record_id=stored_record.record_id,
            payload_sha256=stored_record.payload_sha256,
            stored_record=stored_record,
        )

    # ----------------------------------------------------------------
    # Registry construction
    # ----------------------------------------------------------------

    def handler_mapping(self) -> dict[OrchestrationStage, StageHandler]:
        return {
            OrchestrationStage.INGESTION: self.run_ingestion,
            OrchestrationStage.PREPROCESSING: self.run_preprocessing,
            OrchestrationStage.OCR: self.run_ocr,
            OrchestrationStage.NORMALIZATION: self.run_normalization,
            OrchestrationStage.FINANCIAL_VALIDATION: self.run_financial_validation,
            OrchestrationStage.REFERENCE_MATCHING: self.run_reference_matching,
            OrchestrationStage.MEMORY_PERSISTENCE: self.run_memory_persistence,
        }


def build_stage_handler_registry(
    handlers: dict[OrchestrationStage, StageHandler],
) -> StageHandlerRegistry:
    """Convert a convenient `{stage: handler}` mapping into the ordered
    tuple-of-pairs `StageHandlerRegistry` contract, in the processing
    order the registry itself validates (notebook cell 93's
    `build_stage_handler_registry`, simplified: the notebook's version
    inspects `StageHandlerRegistry.__dataclass_fields__` to guess a
    constructor keyword because it was written before the field name was
    settled; this module defines that dataclass, so the field name is
    already known)."""

    from ap_agent.models.orchestration import ORCHESTRATION_PROCESSING_ORDER

    ordered_handlers = tuple((stage, handlers[stage]) for stage in ORCHESTRATION_PROCESSING_ORDER)

    return StageHandlerRegistry(handlers=ordered_handlers)

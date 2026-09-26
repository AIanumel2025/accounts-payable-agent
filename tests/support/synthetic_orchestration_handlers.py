"""Synthetic, framework-neutral stage-handler registry for M9 orchestration
tests (task §15 "Synthetic orchestration tests"; task §13's batch-200
synthetic load test).

Deliberately not part of `ap_agent.orchestration.handlers` (which binds
the *real* Phase 1-7 tools -- CLAUDE.md: fixtures/synthetic scaffolding
never enter production code). This is the same shape of self-test double
the notebook's own Phase 8 Cell 3 built inline
(`create_synthetic_handler_registry`), moved out of production code and
into test support, per the M9 task brief §15's note that the notebook
cell's inline assertions become ordinary tests rather than module-import
side effects.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional
from uuid import NAMESPACE_URL, UUID, uuid5

from ap_agent.models.orchestration import (
    OrchestrationFailureClass,
    OrchestrationStage,
    ORCHESTRATION_PROCESSING_ORDER,
    StructuredPhaseFailure,
)
from ap_agent.orchestration.engine import StageHandlerRegistry


def synthetic_result_id(document_id: UUID, stage: OrchestrationStage) -> UUID:
    return uuid5(NAMESPACE_URL, f"ap-agent/test/synthetic-result/{document_id}/{stage.value}")


def build_synthetic_handler_registry(
    document_id: UUID,
    *,
    review_stage: Optional[OrchestrationStage] = None,
    skip_stage: Optional[OrchestrationStage] = None,
    transient_failure_stage: Optional[OrchestrationStage] = None,
    permanent_failure_stage: Optional[OrchestrationStage] = None,
    integrity_failure_stage: Optional[OrchestrationStage] = None,
    structured_failure_stage: Optional[OrchestrationStage] = None,
) -> tuple[StageHandlerRegistry, dict[OrchestrationStage, int]]:
    """Build a `StageHandlerRegistry` whose handlers succeed by default
    and can be configured to review/skip/fail at exactly one named stage.

    Returns the registry plus a mutable `{stage: attempt_count}` dict a
    test can assert on afterwards (retry counts, no-downstream-execution
    proof via a zero count).
    """

    attempt_counts: dict[OrchestrationStage, int] = {
        stage: 0 for stage in ORCHESTRATION_PROCESSING_ORDER
    }

    def make_handler(stage: OrchestrationStage):
        def handler(context) -> object:
            attempt_counts[stage] += 1

            if stage == transient_failure_stage and attempt_counts[stage] == 1:
                raise TimeoutError("temporary service timeout")

            if stage == permanent_failure_stage:
                raise ValueError("permanent synthetic failure")

            if stage == integrity_failure_stage:
                raise AssertionError("payload hash mismatch")

            if stage == structured_failure_stage:
                raise StructuredPhaseFailure(
                    stage,
                    errors=(f"{stage.value}_SYNTHETIC_STRUCTURED_ERROR",),
                    review_reasons=(f"{stage.value}_SYNTHETIC_STRUCTURED_REVIEW",),
                    failure_class=OrchestrationFailureClass.INTEGRITY,
                )

            if stage == skip_stage:
                return SimpleNamespace(
                    document_id=document_id,
                    result_id=synthetic_result_id(document_id, stage),
                    status="SKIPPED",
                    review_required=False,
                    review_reasons=tuple(),
                )

            stage_requires_review = stage == review_stage

            if stage == OrchestrationStage.MEMORY_PERSISTENCE:
                stage_requires_review = stage_requires_review or context.review_required

            return SimpleNamespace(
                document_id=document_id,
                result_id=synthetic_result_id(document_id, stage),
                status="REVIEW_REQUIRED" if stage_requires_review else "SUCCEEDED",
                review_required=stage_requires_review,
                review_reasons=(f"{stage.value}_SYNTHETIC_REVIEW",) if stage_requires_review else tuple(),
            )

        return handler

    handlers = tuple((stage, make_handler(stage)) for stage in ORCHESTRATION_PROCESSING_ORDER)

    return StageHandlerRegistry(handlers=handlers), attempt_counts

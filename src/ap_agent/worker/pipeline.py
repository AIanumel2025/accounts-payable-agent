"""Production pipeline construction for the M11D worker.

New in M11D. Builds the *existing* Phase 1-7 tool bindings
(`ap_agent.orchestration.handlers.ProductionPhaseBindings`) for one job --
no phase logic is copied. Phase artifacts live under
`<phase_root>/<tenant>/<job>/...` so jobs never share artifact directories.

Heavy optional dependencies (PaddleOCR, pytesseract, OpenCV) are imported
only inside `OcrEngineProvider.get`/the phase tools, never at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from uuid import UUID

from ap_agent.adapters.reference_data_adapter import load_reference_data_bundle
from ap_agent.config.settings import (
    FinancialValidationConfig,
    IngestionConfig,
    MatchingConfig,
    NormalizationConfig,
    OCRConfig,
    PaddleEngineOptions,
    PreprocessingConfig,
)
from ap_agent.models.matching import ReferenceDataBundle
from ap_agent.orchestration.engine import StageHandlerRegistry
from ap_agent.orchestration.handlers import ProductionPhaseBindings, build_stage_handler_registry
from ap_agent.services.memory_service import MemoryService
from ap_agent.worker.config import WorkerConfig

__all__ = ["PipelineConfigs", "OcrEngineProvider", "build_pipeline_configs", "build_production_registry"]


@dataclass(frozen=True)
class PipelineConfigs:
    ingestion: IngestionConfig
    preprocessing: PreprocessingConfig
    ocr: OCRConfig
    normalization: NormalizationConfig
    financial_validation: FinancialValidationConfig
    matching: MatchingConfig


def build_pipeline_configs(phase_directory: Path, *, ocr_provider: str) -> PipelineConfigs:
    # minimum_width/height mirror the validated M9 four-fixture run.
    return PipelineConfigs(
        ingestion=IngestionConfig(artifact_root=phase_directory / "phase1"),
        preprocessing=PreprocessingConfig(
            artifact_root=phase_directory / "phase2", minimum_width=500, minimum_height=700
        ),
        ocr=(
            OCRConfig(artifact_root=phase_directory / "phase3", ocr_engine_name="paddleocr", ocr_version="ocr-v2-paddle")
            if ocr_provider == "paddleocr"
            else OCRConfig(artifact_root=phase_directory / "phase3", ocr_engine_name="tesseract", ocr_version="ocr-v1")
        ),
        normalization=NormalizationConfig(artifact_root=phase_directory / "phase4"),
        financial_validation=FinancialValidationConfig(artifact_root=phase_directory / "phase5"),
        matching=MatchingConfig(artifact_root=phase_directory / "phase6"),
    )


class _PrimaryProviderDisabled:
    """Stand-in primary engine for a tesseract-only worker: every call fails,
    so the existing Phase 3 router (`extract_routed_ocr_page`) takes its
    Tesseract fallback, exactly as it does when PaddleOCR is unavailable.
    Phase 3 marks fallback pages `REVIEW_REQUIRED` by design (M4)."""

    def predict(self, _path: str):  # noqa: D401
        raise RuntimeError("The primary OCR provider is disabled for this worker.")


class OcrEngineProvider:
    """Lazily constructs (once per worker process) the OCR engine and its
    version string."""

    def __init__(self, provider: str, *, engine_factory: Callable[[], tuple[Any, str]] | None = None) -> None:
        self._provider = provider
        self._engine_factory = engine_factory
        self._engine: tuple[Any, str] | None = None
        self._lock = Lock()

    @property
    def provider(self) -> str:
        return self._provider

    def get(self) -> tuple[Any, str]:
        with self._lock:
            if self._engine is None:
                self._engine = self._build()

            return self._engine

    def _build(self) -> tuple[Any, str]:
        if self._engine_factory is not None:
            return self._engine_factory()

        if self._provider == "paddleocr":
            from ap_agent.adapters.paddleocr_adapter import create_engine, get_paddleocr_version

            return create_engine(PaddleEngineOptions()), get_paddleocr_version()

        from ap_agent.adapters.tesseract_adapter import get_tesseract_version

        return _PrimaryProviderDisabled(), get_tesseract_version()


def load_reference_data(config: WorkerConfig) -> ReferenceDataBundle:
    return load_reference_data_bundle(config.reference_data_directory)


def build_production_registry(
    *,
    configs: PipelineConfigs,
    reference_data: ReferenceDataBundle,
    ocr_engine: Any,
    ocr_engine_version: str,
    memory_service: MemoryService,
) -> tuple[StageHandlerRegistry, ProductionPhaseBindings]:
    bindings = ProductionPhaseBindings(
        ingestion_config=configs.ingestion,
        preprocessing_config=configs.preprocessing,
        ocr_config=configs.ocr,
        normalization_config=configs.normalization,
        financial_validation_config=configs.financial_validation,
        matching_config=configs.matching,
        reference_data=reference_data,
        ocr_engine=ocr_engine,
        ocr_engine_version=ocr_engine_version,
        memory_service=memory_service,
    )

    return build_stage_handler_registry(bindings.handler_mapping()), bindings

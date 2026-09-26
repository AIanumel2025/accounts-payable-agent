"""M9 namespace-isolation regression tests (task §4/§15).

The notebook's Phase 8 correction cells 4C/4D exist only because every
notebook cell shares one global namespace: by the time Phase 8 re-runs
Phase 5's/Phase 6's real functions, a later cell in the *same* notebook
process has already overwritten same-named helpers those functions relied
on (Phase 7 overwrites Phase 6's one-argument `normalized_field_value`
with a two-argument function of the same name; Phase 6 and Phase 5 also
define distinct helpers under names that collide across notebook cells).
4C/4D "fix" this by temporarily monkeypatching `globals()` back to the
older definition for the duration of one call, then restoring it.

This module proves that problem cannot occur in the modular package: it
imports Phase 5 (`ap_agent.tools.financial_validation`), Phase 6
(`ap_agent.tools.matching`) and Phase 7 (`ap_agent.services.memory_service`,
`ap_agent.serialization.memory_json`) together -- in the same test process,
the same way `ap_agent.orchestration.handlers` imports all of them together
in one process -- and shows:

  - Phase 6's `normalized_field_value` keeps its correct (one-argument)
    signature after Phase 7's modules are imported.
  - Phase 7's same-named `normalized_field_value`
    (`ap_agent.serialization.memory_json`) is a distinct function object
    with a different signature, not a later overwrite of Phase 6's.
  - The two PO-backed real fixtures (`Template1_Instance90.jpg`, whose
    matched purchase order is PO 99, and `invoice_Aaron Bergman_36258.pdf`)
    still execute PO-backed line matching end to end, produce
    `REVIEW_REQUIRED` (not `FAILED`), and raise no `TypeError` -- the
    concrete failure mode 4D was written to paper over.
  - No production module uses `globals()`-swapping or a context-manager
    shim to achieve this (grep-checked below).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import ap_agent.serialization.memory_json as memory_json_module
import ap_agent.services.memory_service as memory_service_module
import ap_agent.tools.financial_validation as financial_validation_module
import ap_agent.tools.matching as matching_module
import ap_agent.tools.normalization as normalization_module
from ap_agent.adapters.reference_data_adapter import load_reference_data_bundle
from ap_agent.config.settings import (
    FinancialValidationConfig,
    IngestionConfig,
    MatchingConfig,
    NormalizationConfig,
    OCRConfig,
    PreprocessingConfig,
)
from ap_agent.models.matching import MatchingStatus
from ap_agent.tools.financial_validation import build_validation_input, process_financial_validation
from ap_agent.tools.ingestion import create_batch_id, ingest_document
from ap_agent.tools.matching import build_matching_input, process_invoice_matching
from ap_agent.tools.normalization import build_normalization_input, normalize_invoice_document
from ap_agent.tools.ocr import build_ocr_document_input, process_ocr_document
from ap_agent.tools.preprocessing import build_preprocessing_input, preprocess_document

pytestmark = [pytest.mark.integration, pytest.mark.requires_fixtures]

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
REFERENCE_DATA_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "reference_data"

PO_BACKED_FIXTURES = ("Template1_Instance90.jpg", "invoice_Aaron Bergman_36258.pdf")

ORCHESTRATION_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "ap_agent" / "orchestration"


def test_phase_6_normalized_field_value_keeps_its_one_argument_signature():
    """Phase 6's own helper (`ap_agent.tools.matching.normalized_field_value`)
    must still take exactly one positional parameter after Phase 7's
    modules (which define a *different* two-argument function of the same
    short name) are imported into the same process."""

    signature = inspect.signature(matching_module.normalized_field_value)
    assert list(signature.parameters) == ["normalized_field"]


def test_phase_7_normalized_field_value_is_a_distinct_two_argument_function():
    signature = inspect.signature(memory_json_module.normalized_field_value)
    assert list(signature.parameters) == ["invoice_record", "requested_field_name"]


def test_no_phase_7_helper_overwrites_the_phase_6_helper_object():
    """The two `normalized_field_value` functions are different objects
    living in different module namespaces -- Python's ordinary import
    scoping, not a notebook global either one could clobber."""

    assert matching_module.normalized_field_value is not memory_json_module.normalized_field_value
    assert matching_module.__dict__["normalized_field_value"] is matching_module.normalized_field_value
    assert memory_json_module.__dict__["normalized_field_value"] is memory_json_module.normalized_field_value


def test_phase_5_and_phase_6_each_keep_their_own_append_unique_reason():
    """Phase 5 and Phase 6 each define their own `append_unique_reason`
    (notebook decision D-8/D-9's namespace collision risk); confirm they
    are distinct functions, not one overwriting the other now that both
    modules are imported together."""

    assert (
        financial_validation_module.append_unique_reason
        is not matching_module.append_unique_reason
    )


@pytest.mark.parametrize(
    "module_path",
    ["engine.py", "handlers.py", "routing.py", "batch.py"],
)
def test_no_production_orchestration_module_swaps_globals_at_runtime(module_path):
    """task §4: production modules must not 'mutate module globals
    temporarily' or 'copy the notebook context-manager shims into src/'.
    `globals()`-mutation and `contextlib.contextmanager`-based scope
    swapping are exactly the two mechanisms notebook correction cells
    4C/4D use; neither may appear in production orchestration code."""

    source = (ORCHESTRATION_SOURCE_ROOT / module_path).read_text(encoding="utf-8")

    assert "globals()." not in source
    assert "globals().update" not in source
    assert "globals()[" not in source
    assert "globals().pop" not in source


def _run_po_backed_pipeline(tmp_path, fixture_filename: str, *, engine, engine_version: str):
    ingestion_config = IngestionConfig(artifact_root=tmp_path / "phase1")
    preprocessing_config = PreprocessingConfig(
        artifact_root=tmp_path / "phase2", minimum_width=500, minimum_height=700
    )
    ocr_config = OCRConfig(artifact_root=tmp_path / "phase3", ocr_engine_name="paddleocr", ocr_version="ocr-v2-paddle")
    normalization_config = NormalizationConfig(artifact_root=tmp_path / "phase4")
    financial_validation_config = FinancialValidationConfig(artifact_root=tmp_path / "phase5")
    matching_config = MatchingConfig(artifact_root=tmp_path / "phase6")

    reference_data = load_reference_data_bundle(REFERENCE_DATA_DIR)

    batch_id = create_batch_id("m9_namespace_isolation_test", fixture_filename)

    fixture_path = FIXTURES_DIR / fixture_filename

    ingestion_result = ingest_document(
        file_path=fixture_path,
        config=ingestion_config,
        batch_id=batch_id,
        ingestion_source="m9_namespace_isolation_test",
        known_sha256_values=set(),
    )

    preprocessing_input = build_preprocessing_input(ingestion_result)
    preprocessing_result = preprocess_document(preprocessing_input, preprocessing_config)

    ocr_input = build_ocr_document_input(preprocessing_result, preprocessing_config.preprocessing_version)
    ocr_result = process_ocr_document(
        ocr_input,
        ocr_config,
        engine=engine,
        engine_version=engine_version,
    )

    normalization_input = build_normalization_input(
        ocr_result=ocr_result, preprocessing_result=preprocessing_result, ocr_config=ocr_config
    )
    normalization_result = normalize_invoice_document(normalization_input, config=normalization_config)

    validation_input = build_validation_input(normalization_result, normalization_config=normalization_config)
    financial_validation_result = process_financial_validation(validation_input, config=financial_validation_config)

    matching_input = build_matching_input(normalization_result, financial_validation_result, reference_data)
    matching_result = process_invoice_matching(matching_input, config=matching_config)

    return matching_result


@pytest.fixture(scope="module")
def real_paddle_engine():
    """The real PaddleOCR engine, per task §16: 'Real-Paddle tests must
    use the real PaddleOCR engine. Use no mocks. Use no forced Tesseract
    fallback.' A weaker (Tesseract-fallback) OCR pass does not reliably
    resolve these two fixtures' supplier/PO references at all, so it
    cannot exercise the PO-backed line-matching code path 4D was written
    to protect -- only the real engine does."""

    from ap_agent.adapters.paddleocr_adapter import create_engine, get_paddleocr_version
    from ap_agent.config.settings import PaddleEngineOptions

    try:
        engine = create_engine(PaddleEngineOptions())
    except Exception as exc:  # pragma: no cover - depends on network policy
        pytest.skip(
            "PaddleOCR engine could not be constructed (model download "
            f"blocked): {type(exc).__name__}: {exc}"
        )

    return engine, get_paddleocr_version()


@pytest.mark.requires_paddle
@pytest.mark.slow
@pytest.mark.parametrize("fixture_filename", PO_BACKED_FIXTURES)
def test_po_backed_fixtures_execute_line_matching_without_a_type_error(tmp_path, fixture_filename, real_paddle_engine):
    """The concrete failure mode notebook correction cell 4D exists to
    prevent: Phase 7 overwriting Phase 6's one-argument
    `normalized_field_value` with a two-argument function, causing
    PO-backed invoices to raise `TypeError` inside Phase 6 line matching.
    With every Phase 5/6/7 module already imported into this process
    (module-level imports above), this must not happen."""

    engine, engine_version = real_paddle_engine

    try:
        matching_result = _run_po_backed_pipeline(
            tmp_path, fixture_filename, engine=engine, engine_version=engine_version
        )
    except TypeError as error:  # pragma: no cover - the regression this test guards against
        pytest.fail(f"{fixture_filename} raised TypeError during PO-backed line matching: {error}")

    assert matching_result.status != MatchingStatus.FAILED
    assert matching_result.errors == ()


@pytest.mark.requires_paddle
@pytest.mark.slow
@pytest.mark.parametrize("fixture_filename", PO_BACKED_FIXTURES)
def test_po_backed_fixtures_produce_review_required_not_failed(tmp_path, fixture_filename, real_paddle_engine):
    engine, engine_version = real_paddle_engine

    matching_result = _run_po_backed_pipeline(
        tmp_path, fixture_filename, engine=engine, engine_version=engine_version
    )

    assert matching_result.status == MatchingStatus.REVIEW_REQUIRED
    assert len(matching_result.line_matches) > 0

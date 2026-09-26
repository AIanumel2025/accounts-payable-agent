"""M9 §2/§15 four-fixture acceptance: the modular Phase 1-7 orchestration
path, exercised through the real modular tools (not the notebook), over
all four controlled invoice fixtures, asserted against the exact golden
table in the M9 task brief §2 / `tests/golden/phase_8_expected_results.json`.

Uses the real PaddleOCR engine (task §16: 'Real-Paddle tests must use the
real PaddleOCR engine. Use no mocks. Use no forced Tesseract fallback.'),
because the golden table's specific outcomes (Template1_Instance90.jpg's
PO 99 match, invoice_Aaron Bergman_36258.pdf's PO match, 08181_flat_document
.png's SUP-001 supplier match) depend on PaddleOCR's OCR quality -- the
same fixtures run through the Tesseract fallback do not reliably resolve
the same supplier/PO references (see
tests/integration/test_orchestration_namespace_isolation.py's module
docstring). Marked `requires_paddle`; skips (not fails) when the
environment cannot construct a real PaddleOCR engine.

Stage 7 (MEMORY_PERSISTENCE) goes through the real
`ap_agent.services.memory_service.MemoryService` backed by
`tests.support.fake_memory_repository.FakeMemoryRepository` (no `psycopg`),
matching `tests/integration/test_memory_golden.py`'s precedent: no live
PostgreSQL instance is available in this environment (see
`tests/integration/test_memory_postgres_integration.py`, `requires_postgres`,
and `docs/m8_phase_7_postgres_memory_report.md`). This exercises the real
`ap_agent.orchestration.handlers.ProductionPhaseBindings.run_memory_persistence`
code path end to end (alignment, deterministic IDs, hashing, append-only
storage) -- only the underlying repository implementation differs from a
real-database run.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

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
from ap_agent.models.normalization import InvoiceFieldName
from ap_agent.models.orchestration import (
    InvoiceWorkflowRequest,
    InvoiceWorkflowStatus,
    OrchestrationStage,
    ORCHESTRATION_PROCESSING_ORDER,
    default_orchestration_config,
)
from ap_agent.orchestration.engine import execute_invoice_workflow
from ap_agent.orchestration.handlers import ProductionPhaseBindings, build_stage_handler_registry
from ap_agent.services.memory_service import MemoryService
from ap_agent.tools.financial_validation import get_invoice_field_value
from tests.support.fake_memory_repository import FakeMemoryRepository

pytestmark = [pytest.mark.integration, pytest.mark.requires_fixtures, pytest.mark.requires_paddle, pytest.mark.slow]

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
REFERENCE_DATA_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "reference_data"
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "golden" / "phase_8_expected_results.json"

# The four controlled fixtures, in the order the M9 task brief §2's golden
# table lists them.
FIXTURE_FILENAMES = (
    "Template1_Instance90.jpg",
    "08181_flat_document.png",
    "invoice_Aaron Bergman_36258.pdf",
    "08181_warped_document_perspective_shadow.jpg",
)

TENANT_ID = uuid5(NAMESPACE_URL, "ap-agent/test/m9-orchestration-tenant")


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def real_paddle_engine():
    from ap_agent.adapters.paddleocr_adapter import create_engine, get_paddleocr_version

    try:
        engine = create_engine(PaddleEngineOptions())
    except Exception as exc:  # pragma: no cover - depends on network policy
        pytest.skip(
            "PaddleOCR engine could not be constructed (model download "
            f"blocked): {type(exc).__name__}: {exc}"
        )

    return engine, get_paddleocr_version()


@pytest.fixture(scope="module")
def orchestration_run(tmp_path_factory, real_paddle_engine):
    """Run all four fixtures through the real modular Phase 1-7
    orchestration path exactly once for this module, so every assertion
    test below reuses the same run instead of repeating an ~2-minute
    PaddleOCR pass per assertion."""

    tmp_path = tmp_path_factory.mktemp("phase_8_four_fixtures")
    engine, engine_version = real_paddle_engine

    ingestion_config = IngestionConfig(artifact_root=tmp_path / "phase1")
    preprocessing_config = PreprocessingConfig(
        artifact_root=tmp_path / "phase2", minimum_width=500, minimum_height=700
    )
    ocr_config = OCRConfig(artifact_root=tmp_path / "phase3", ocr_engine_name="paddleocr", ocr_version="ocr-v2-paddle")
    normalization_config = NormalizationConfig(artifact_root=tmp_path / "phase4")
    financial_validation_config = FinancialValidationConfig(artifact_root=tmp_path / "phase5")
    matching_config = MatchingConfig(artifact_root=tmp_path / "phase6")

    reference_data = load_reference_data_bundle(REFERENCE_DATA_DIR)

    repository = FakeMemoryRepository()
    memory_service = MemoryService(repository, tenant_id=TENANT_ID)

    bindings = ProductionPhaseBindings(
        ingestion_config=ingestion_config,
        preprocessing_config=preprocessing_config,
        ocr_config=ocr_config,
        normalization_config=normalization_config,
        financial_validation_config=financial_validation_config,
        matching_config=matching_config,
        reference_data=reference_data,
        ocr_engine=engine,
        ocr_engine_version=engine_version,
        memory_service=memory_service,
    )

    registry = build_stage_handler_registry(bindings.handler_mapping())
    config = default_orchestration_config()

    batch_id = uuid4()
    submitted_at = datetime.now(timezone.utc)

    results = {}
    for filename in FIXTURE_FILENAMES:
        request = InvoiceWorkflowRequest(
            tenant_id=TENANT_ID,
            batch_id=batch_id,
            correlation_id=uuid5(NAMESPACE_URL, f"ap-agent/test/m9-four-fixtures/{batch_id}/{filename}"),
            source_path=FIXTURES_DIR / filename,
            source_channel="M9_FOUR_FIXTURE_TEST",
            submitted_at=submitted_at,
        )

        results[filename] = execute_invoice_workflow(request=request, handler_registry=registry, config=config)

    return {
        "results": results,
        "repository": repository,
        "memory_service": memory_service,
        "batch_id": batch_id,
    }


def test_no_unhandled_exceptions_across_all_four_fixtures(orchestration_run):
    # execute_invoice_workflow only raises AssertionError for an internal
    # engine-graph defect; every legitimate stage failure is caught and
    # converted to a terminal FAILED result instead. Reaching this line at
    # all (the fixture above did not raise) is therefore the check.
    assert len(orchestration_run["results"]) == 4


@pytest.mark.parametrize("filename", FIXTURE_FILENAMES)
def test_fixture_matches_the_golden_table(orchestration_run, golden, filename):
    result = orchestration_run["results"][filename]
    expected = next(doc for doc in golden["documents"] if doc["filename"] == filename)

    assert result.status.value == expected["workflow_status"]
    assert result.terminal_route == expected["terminal_route"]
    assert result.review_required is expected["review_required"]
    assert len(result.stage_records) == expected["stages_executed"]

    failed_stages = [r for r in result.stage_records if r.status.value == "FAILED"]
    assert len(failed_stages) == expected["failed_stages"]

    matching_result = result.matching_result
    assert matching_result is not None
    assert matching_result.purchase_order_resolution.status.value == expected["purchase_order_status"]
    assert matching_result.purchase_order_resolution.purchase_order_number == expected["purchase_order_number"]
    assert matching_result.supplier_resolution.status.value == expected["supplier_status"]
    assert matching_result.supplier_resolution.matched_supplier_id == expected["supplier_id"]


def test_seven_stages_for_every_invoice(orchestration_run):
    for filename, result in orchestration_run["results"].items():
        visited = [record.stage for record in result.stage_records]
        assert visited == list(ORCHESTRATION_PROCESSING_ORDER), filename


def test_zero_failed_stages_across_all_invoices(orchestration_run):
    for filename, result in orchestration_run["results"].items():
        assert all(record.status.value != "FAILED" for record in result.stage_records), filename
        assert result.errors == (), filename


def test_aggregate_counts_match_the_golden_table(orchestration_run, golden):
    results = list(orchestration_run["results"].values())
    aggregate = golden["aggregate_expected"]

    assert len(results) == aggregate["invoices_orchestrated"]
    assert sum(r.status == InvoiceWorkflowStatus.SUCCEEDED for r in results) == aggregate["completed_automatically"]
    assert (
        sum(r.status == InvoiceWorkflowStatus.REVIEW_REQUIRED for r in results)
        == aggregate["routed_to_human_review"]
    )
    assert sum(r.status == InvoiceWorkflowStatus.FAILED for r in results) == aggregate["failed_workflows"]


def test_four_postgresql_memory_writes(orchestration_run, golden):
    """One append-only memory record was written per invoice (task §2:
    'PostgreSQL memory writes: 4') -- via `FakeMemoryRepository` here (no
    live PostgreSQL in this environment; see module docstring), which
    reproduces the same `MemoryRepository` contract
    `ap_agent.repositories.postgres_memory_repository.PostgresMemoryRepository`
    does."""

    repository = orchestration_run["repository"]
    stored_count = sum(
        1 for (tenant_id, _document_id) in repository._invoice_memory if tenant_id == TENANT_ID
    )

    assert stored_count == golden["aggregate_expected"]["postgresql_memory_writes"]


def test_no_cross_document_leakage(orchestration_run):
    document_ids = {
        filename: result.document_id for filename, result in orchestration_run["results"].items()
    }

    assert len(set(document_ids.values())) == 4

    for filename, result in orchestration_run["results"].items():
        other_ids = {str(doc_id) for name, doc_id in document_ids.items() if name != filename}

        for stage_record in result.stage_records:
            if stage_record.result_id:
                assert stage_record.result_id not in other_ids


def test_no_cross_tenant_leakage(orchestration_run):
    other_tenant_id = uuid4()
    memory_service = orchestration_run["memory_service"]
    other_tenant_service = MemoryService(orchestration_run["repository"], tenant_id=other_tenant_id)

    for filename, result in orchestration_run["results"].items():
        assert other_tenant_service.retrieve_bundle(document_id=result.document_id) is None
        assert memory_service.retrieve_bundle(document_id=result.document_id) is not None


def test_stable_document_identities_and_source_hashes(orchestration_run):
    for filename, result in orchestration_run["results"].items():
        assert result.ingestion_result.identity.document_id == result.document_id
        assert result.preprocessing_result.document_id == result.document_id
        assert result.ocr_result.document_id == result.document_id
        assert result.normalization_result.document_id == result.document_id
        assert result.financial_validation_result.document_id == result.document_id
        assert result.matching_result.document_id == result.document_id

        source_hashes = {
            result.ingestion_result.identity.sha256,
            result.preprocessing_result.source_sha256,
            result.ocr_result.source_document_sha256,
            result.normalization_result.source_document_sha256,
            result.financial_validation_result.source_document_sha256,
            result.matching_result.source_document_sha256,
        }
        assert len(source_hashes) == 1, f"{filename}: source hash discontinuity across phases"


def test_persisted_and_in_memory_results_agree(orchestration_run):
    memory_service = orchestration_run["memory_service"]

    for filename, result in orchestration_run["results"].items():
        bundle = memory_service.retrieve_bundle(document_id=result.document_id)
        assert bundle is not None
        assert bundle.invoice_memory is not None
        assert bundle.invoice_memory.payload_sha256 == result.memory_bundle.payload_sha256
        assert bundle.invoice_memory.review_required == result.memory_bundle.review_required


def test_template_po_99_remains_matched(orchestration_run):
    result = orchestration_run["results"]["Template1_Instance90.jpg"]
    assert result.matching_result.purchase_order_resolution.purchase_order_number == "99"


def test_aaron_po_remains_matched(orchestration_run):
    result = orchestration_run["results"]["invoice_Aaron Bergman_36258.pdf"]
    assert result.matching_result.purchase_order_resolution.purchase_order_number is not None
    assert result.matching_result.purchase_order_resolution.status.value == "MATCHED"


def test_flat_supplier_remains_sup_001(orchestration_run):
    result = orchestration_run["results"]["08181_flat_document.png"]
    assert result.matching_result.supplier_resolution.matched_supplier_id == "SUP-001"


def test_missing_values_remain_missing(orchestration_run):
    template_result = orchestration_run["results"]["Template1_Instance90.jpg"]
    supplier_name = get_invoice_field_value(
        template_result.normalization_result.invoice_record, InvoiceFieldName.SUPPLIER_NAME
    )
    assert supplier_name is None

    aaron_result = orchestration_run["results"]["invoice_Aaron Bergman_36258.pdf"]
    aaron_supplier_name = get_invoice_field_value(
        aaron_result.normalization_result.invoice_record, InvoiceFieldName.SUPPLIER_NAME
    )
    assert aaron_supplier_name is None


def test_no_inferred_warped_total(orchestration_run):
    """The warped/perspective/shadow fixture's grand total is not visibly
    captured by OCR; Phase 4/8 must never fabricate one (D-4/D-5)."""

    warped_result = orchestration_run["results"]["08181_warped_document_perspective_shadow.jpg"]
    total_amount = get_invoice_field_value(
        warped_result.normalization_result.invoice_record, InvoiceFieldName.TOTAL_AMOUNT
    )
    assert total_amount is None

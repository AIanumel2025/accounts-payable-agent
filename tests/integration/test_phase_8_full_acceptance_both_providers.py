"""M9 §20 step 8: the full Phase 1-8 acceptance run requiring *both*
providers -- the real PaddleOCR engine (task §16) and a real PostgreSQL
instance for Stage 7 (task §16, `requires_postgres`), rather than
`tests/integration/test_phase_8_orchestration_four_fixtures.py`'s
`FakeMemoryRepository` substitute for Stage 7.

This repeats that module's four-fixture run with one substitution: Stage 7
goes through the real `ap_agent.repositories.postgres_memory_repository
.PostgresMemoryRepository`, using the same `AP_AGENT_TEST_POSTGRES_DSN`
sourcing, database-identity gate, and least-privilege runtime role as
`tests/integration/test_memory_postgres_integration.py` (see that module's
docstring for why: this suite must never fall back to the production DSN
env vars, and must never run against a database that might not be the
disposable `ap_agent_m8_test` instance).

Marked `requires_paddle` **and** `requires_postgres`: it skips (never
fails) whenever either provider is unavailable, and is therefore not part
of the default `-m "not requires_paddle and not requires_postgres"` fast
suite, nor of a `-m requires_paddle`-only or `-m requires_postgres`-only
run. See docs/m9_phase_8_orchestration_report.md §16 for this run's
result: `AP_AGENT_TEST_POSTGRES_DSN` was unset in this environment, so this
suite collected but did not execute here.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

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
from ap_agent.models.orchestration import (
    InvoiceWorkflowRequest,
    InvoiceWorkflowStatus,
    default_orchestration_config,
)
from ap_agent.orchestration.engine import execute_invoice_workflow
from ap_agent.orchestration.handlers import ProductionPhaseBindings, build_stage_handler_registry
from ap_agent.services.memory_service import MemoryService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_fixtures,
    pytest.mark.requires_paddle,
    pytest.mark.requires_postgres,
    pytest.mark.slow,
]

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
REFERENCE_DATA_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "reference_data"

FIXTURE_FILENAMES = (
    "Template1_Instance90.jpg",
    "08181_flat_document.png",
    "invoice_Aaron Bergman_36258.pdf",
    "08181_warped_document_perspective_shadow.jpg",
)

TEST_DSN_ENV_VAR = "AP_AGENT_TEST_POSTGRES_DSN"
EXPECTED_TEST_DATABASE = "ap_agent_m8_test"


def _owner_dsn_or_skip() -> str:
    """Reproduces `test_memory_postgres_integration.py::_owner_dsn_or_skip`'s
    fail-closed database-identity gate. Duplicated rather than imported: a
    cross-file pytest fixture import would make this module collect-time
    dependent on that file's fixture graph, which is unnecessary coupling
    for what is otherwise an independent acceptance run."""

    dsn = os.environ.get(TEST_DSN_ENV_VAR, "").strip()

    if not dsn:
        pytest.skip(
            f"{TEST_DSN_ENV_VAR} is not configured; this full both-providers "
            "acceptance run requires a real, disposable PostgreSQL test "
            "database (see docs/m8_phase_7_postgres_memory_report.md)."
        )

    from psycopg.conninfo import conninfo_to_dict

    dbname = conninfo_to_dict(dsn).get("dbname")

    if dbname != EXPECTED_TEST_DATABASE:
        pytest.fail(
            f"{TEST_DSN_ENV_VAR} targets database {dbname!r}, expected "
            f"{EXPECTED_TEST_DATABASE!r}. Refusing to run against a database "
            "that might not be the disposable test instance."
        )

    return dsn


@pytest.fixture(scope="module")
def real_paddle_engine():
    from ap_agent.adapters.paddleocr_adapter import create_engine, get_paddleocr_version

    try:
        engine = create_engine(PaddleEngineOptions())
    except Exception as exc:  # pragma: no cover - depends on network policy
        pytest.skip(f"PaddleOCR engine could not be constructed: {type(exc).__name__}: {exc}")

    return engine, get_paddleocr_version()


@pytest.fixture(scope="module")
def real_postgres_memory_service():
    from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    dsn = _owner_dsn_or_skip()
    config = MemoryConfig(transport_policy=PostgresTransportPolicy(require_ssl=True, require_channel_binding=True))

    repository = PostgresMemoryRepository(dsn, config)
    tenant_id = uuid4()
    repository.register_tenant(
        tenant_id=tenant_id,
        tenant_key=f"m9-both-providers-{tenant_id.hex[:8]}",
        display_name="M9 Both-Providers Acceptance Tenant",
    )

    return MemoryService(repository, tenant_id=tenant_id)


def test_full_pipeline_with_real_paddleocr_and_real_postgres(
    tmp_path, real_paddle_engine, real_postgres_memory_service
):
    engine, engine_version = real_paddle_engine
    memory_service = real_postgres_memory_service

    ingestion_config = IngestionConfig(artifact_root=tmp_path / "phase1")
    preprocessing_config = PreprocessingConfig(
        artifact_root=tmp_path / "phase2", minimum_width=500, minimum_height=700
    )
    ocr_config = OCRConfig(artifact_root=tmp_path / "phase3", ocr_engine_name="paddleocr", ocr_version="ocr-v2-paddle")
    normalization_config = NormalizationConfig(artifact_root=tmp_path / "phase4")
    financial_validation_config = FinancialValidationConfig(artifact_root=tmp_path / "phase5")
    matching_config = MatchingConfig(artifact_root=tmp_path / "phase6")

    reference_data = load_reference_data_bundle(REFERENCE_DATA_DIR)

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
            tenant_id=memory_service.tenant_id,
            batch_id=batch_id,
            correlation_id=uuid5(NAMESPACE_URL, f"ap-agent/test/m9-both-providers/{batch_id}/{filename}"),
            source_path=FIXTURES_DIR / filename,
            source_channel="M9_BOTH_PROVIDERS_ACCEPTANCE_TEST",
            submitted_at=submitted_at,
        )
        results[filename] = execute_invoice_workflow(request=request, handler_registry=registry, config=config)

    assert len(results) == 4
    assert sum(r.status == InvoiceWorkflowStatus.SUCCEEDED for r in results.values()) == 1
    assert sum(r.status == InvoiceWorkflowStatus.REVIEW_REQUIRED for r in results.values()) == 3
    assert sum(r.status == InvoiceWorkflowStatus.FAILED for r in results.values()) == 0

    for filename, result in results.items():
        assert len(result.stage_records) == 7, filename
        assert all(record.status.value != "FAILED" for record in result.stage_records), filename

        bundle = memory_service.retrieve_bundle(document_id=result.document_id)
        assert bundle is not None, filename
        assert bundle.invoice_memory is not None, filename

"""M11D Core: the worker with the REAL PaddleOCR provider (real PostgreSQL).

Runs only where PaddleOCR can be constructed (the repository's OCR-capable
`m8-postgres-acceptance.yml` workflow); everywhere else it is reported as
SKIPPED -- never as passed. It proves the upload -> worker path with the
primary OCR provider, end to end, against the golden outcomes recorded in
`tests/golden/phase_8_expected_results.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap_agent.config.settings import PaddleEngineOptions
from ap_agent.models.operations import WorkflowJobStatus
from tests.api.test_m11d_documents_postgres import _registry_builder, _upload
from tests.support.m11d_harness import Db, make_client, make_runner
from ap_agent.worker.pipeline import OcrEngineProvider

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres, pytest.mark.requires_paddle, pytest.mark.slow]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
GOLDEN = json.loads((Path(__file__).resolve().parents[1] / "golden" / "phase_8_expected_results.json").read_text())


@pytest.fixture(scope="module")
def paddle_provider():
    try:
        from ap_agent.adapters.paddleocr_adapter import create_engine, get_paddleocr_version

        engine = create_engine(PaddleEngineOptions())
        version = get_paddleocr_version()
    except Exception as error:  # pragma: no cover - depends on the environment
        pytest.skip(f"PaddleOCR could not be constructed here ({type(error).__name__}); this is a skip, not a pass.")

    return OcrEngineProvider("paddleocr", engine_factory=lambda: (engine, version))


@pytest.mark.parametrize("filename", ["08181_flat_document.png", "Template1_Instance90.jpg"])
def test_real_paddleocr_worker_matches_the_golden_outcome(
    filename, paddle_provider, runtime_dsn, local_config, tenant_id, tmp_path
):
    from ap_agent.adapters.reference_data_adapter import load_reference_data_bundle
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository
    from ap_agent.services.memory_service import MemoryService
    from ap_agent.worker.pipeline import build_pipeline_configs, build_production_registry
    from tests.support.m11d_seed import REFERENCE_DATA_DIRECTORY

    expected = next(document for document in GOLDEN["documents"] if document["filename"] == filename)
    reference_data = load_reference_data_bundle(REFERENCE_DATA_DIRECTORY)

    def build(job):
        engine, version = paddle_provider.get()
        registry, _ = build_production_registry(
            configs=build_pipeline_configs(tmp_path / "phases" / job.job_id.hex, ocr_provider="paddleocr"),
            reference_data=reference_data, ocr_engine=engine, ocr_engine_version=version,
            memory_service=MemoryService(PostgresMemoryRepository(runtime_dsn, local_config), tenant_id=job.tenant_id),
        )
        return registry

    with make_client(runtime_dsn, local_config, tmp_path / "artifacts") as client:
        _upload(client, tenant_id, FIXTURES / filename)

    finished = make_runner(
        runtime_dsn, local_config, tenant_id=tenant_id, work_directory=tmp_path, artifact_root=tmp_path / "artifacts",
        registry_builder=build,
    ).run_once()

    assert finished is not None
    assert finished.status == WorkflowJobStatus(expected["workflow_status"]), (finished.error_code, finished.result_summary)
    assert finished.result_summary["supplier_status"] == expected["supplier_status"]
    assert finished.result_summary["purchase_order_status"] == expected["purchase_order_status"]

    if expected["review_required"]:
        assert finished.result_review_id is not None
    else:
        assert finished.result_review_id is None and Db(runtime_dsn, local_config, tenant_id).count("review_cases") == 0

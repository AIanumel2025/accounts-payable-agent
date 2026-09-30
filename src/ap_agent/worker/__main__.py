"""`python -m ap_agent.worker [--once]` -- the M11D Core worker entry point.

Fail-closed: refuses to start unless `AP_AGENT_ENABLE_WORKER_EXECUTION` is
explicitly truthy. Prints only safe status lines (never a DSN, path or
invoice content). Supports exactly one active worker (see
`ap_agent.worker.runner`).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

EXIT_OK = 0
EXIT_DISABLED = 3
EXIT_CONFIGURATION = 4


def build_runner(config):
    from ap_agent.artifacts.storage import LocalFilesystemArtifactStore
    from ap_agent.config.postgres import MemoryConfig
    from ap_agent.db.connection import load_dsn
    from ap_agent.repositories.operations_repository import OperationsRepository
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository
    from ap_agent.services.document_executor import DocumentJobExecutor
    from ap_agent.services.memory_service import MemoryService
    from ap_agent.services.resume_executor import ResumeJobExecutor
    from ap_agent.worker.pipeline import (
        OcrEngineProvider,
        build_pipeline_configs,
        build_production_registry,
        load_reference_data,
    )
    from ap_agent.worker.runner import WorkerRunner

    memory_config = MemoryConfig()
    dsn = load_dsn(memory_config)
    operations = OperationsRepository(dsn, memory_config)
    store = LocalFilesystemArtifactStore(config.artifact_root)
    ocr_provider = OcrEngineProvider(config.ocr_provider)
    reference_data = load_reference_data(config)

    def phase_directory(job):
        return config.phase_artifact_root / job.tenant_id.hex / job.job_id.hex

    def registry_builder(job):
        engine, engine_version = ocr_provider.get()
        memory_service = MemoryService(PostgresMemoryRepository(dsn, memory_config), tenant_id=job.tenant_id)
        registry, _ = build_production_registry(
            configs=build_pipeline_configs(phase_directory(job), ocr_provider=config.ocr_provider),
            reference_data=reference_data,
            ocr_engine=engine,
            ocr_engine_version=engine_version,
            memory_service=memory_service,
        )
        return registry

    document_executor = DocumentJobExecutor(
        operations=operations, artifact_store=store, registry_builder=registry_builder
    )
    resume_executor = ResumeJobExecutor(
        dsn=dsn,
        memory_config=memory_config,
        operations=operations,
        reference_data=reference_data,
        phase_directory_for=phase_directory,
        ocr_provider_name=config.ocr_provider,
    )

    return WorkerRunner(
        operations=operations,
        document_executor=document_executor,
        resume_executor=resume_executor,
        settings=config.settings,
        tenant_scope=config.tenant_scope,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ap_agent.worker", description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="Process at most one queued job, then exit.")
    arguments = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from ap_agent.worker.config import WorkerConfigurationError, load_worker_config

    try:
        config = load_worker_config()
    except WorkerConfigurationError as error:
        print(f"worker: configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION

    if not config.execution_enabled:
        print(
            "worker: execution is disabled (set AP_AGENT_ENABLE_WORKER_EXECUTION=true to enable).",
            file=sys.stderr,
        )
        return EXIT_DISABLED

    from ap_agent.exceptions import PostgresConfigurationError

    try:
        runner = build_runner(config)
    except PostgresConfigurationError as error:
        # The message names the variable, never its value.
        print(f"worker: database configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION

    if arguments.once:
        finished = runner.run_once()
        print("worker: no queued job." if finished is None else f"worker: job finished as {finished.status.value}.")
        return EXIT_OK

    stop = threading.Event()

    def request_stop(_signal_number, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    print("worker: started (single worker; Ctrl+C to stop after the current job).", flush=True)
    processed = runner.run_forever(stop)
    print(f"worker: stopped after {processed} job(s).", flush=True)

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

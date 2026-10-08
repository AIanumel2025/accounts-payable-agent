"""`python -m ap_agent.worker [--once]` -- the M11D Core worker entry point.

Fail-closed: refuses to start unless `AP_AGENT_ENABLE_WORKER_EXECUTION` is
explicitly truthy. Prints only safe status lines (never a DSN, path or
invoice content). Supports exactly one active worker (see
`ap_agent.worker.runner`).

M11E: reads uploads from the configured artifact store (local filesystem, or
S3-compatible object storage such as Cloudflare R2). In the `hosted`
environment it requires object storage and PaddleOCR, builds the OCR engine
at startup so a missing or broken provider fails clearly *before* any job is
claimed, removes each job's scratch directory when the job finishes, and
shuts down gracefully on SIGTERM (the in-flight job always finishes first).
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
EXIT_OCR_UNAVAILABLE = 5


class OcrUnavailableError(Exception):
    """The OCR provider could not be constructed at startup."""


class _ScratchCleaningExecutor:
    """Removes a job's phase-artifact scratch directory after it runs.
    Used only with object storage, where nothing durable lives there."""

    def __init__(self, inner, directory_for) -> None:
        self._inner = inner
        self._directory_for = directory_for

    def execute(self, job):
        import shutil

        try:
            return self._inner.execute(job)
        finally:
            shutil.rmtree(self._directory_for(job), ignore_errors=True)


def build_runner(config, *, warm_up_ocr: bool = False):
    from ap_agent.artifacts.factory import build_artifact_store
    from ap_agent.config.deployment import StorageMode
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
    store = build_artifact_store(config.storage_mode, artifact_root=config.artifact_root, s3=config.s3, aws_s3=config.aws_s3)
    ocr_provider = OcrEngineProvider(config.ocr_provider)

    if warm_up_ocr:
        try:
            ocr_provider.get()  # fails here, before any job is claimed, if the provider is unusable
        except Exception as error:  # noqa: BLE001 - reported by type only
            raise OcrUnavailableError(type(error).__name__) from error
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

    if config.storage_mode is StorageMode.S3:
        document_executor = _ScratchCleaningExecutor(document_executor, phase_directory)
        resume_executor = _ScratchCleaningExecutor(resume_executor, phase_directory)

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

    from ap_agent.config.deployment import HostedConfigurationError, describe_configuration_status
    from ap_agent.worker.config import WorkerConfigurationError, load_worker_config

    try:
        config = load_worker_config()
    except WorkerConfigurationError as error:
        print(f"worker: configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION
    except HostedConfigurationError as error:
        # Problem codes name settings, never values.
        print(f"worker: configuration error: {', '.join(error.problems)}", file=sys.stderr)
        return EXIT_CONFIGURATION

    print(
        "worker: configuration status: "
        + ", ".join(f"{name}={value}" for name, value in sorted(describe_configuration_status().items())),
        flush=True,
    )

    if not config.execution_enabled:
        print(
            "worker: execution is disabled (set AP_AGENT_ENABLE_WORKER_EXECUTION=true to enable).",
            file=sys.stderr,
        )
        return EXIT_DISABLED

    from ap_agent.exceptions import PostgresConfigurationError

    try:
        runner = build_runner(config, warm_up_ocr=config.hosted)
    except PostgresConfigurationError as error:
        # The message names the variable, never its value.
        print(f"worker: database configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION
    except OcrUnavailableError as error:
        print(f"worker: the OCR provider could not be initialised ({error}).", file=sys.stderr)
        return EXIT_OCR_UNAVAILABLE

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

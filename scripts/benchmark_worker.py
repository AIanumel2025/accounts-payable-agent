#!/usr/bin/env python3
"""Representative worker benchmark with the REAL PaddleOCR provider (M11E.1).

Answers one question for the AWS deployment: can the OCR worker finish a representative invoice comfortably inside
AWS Lambda's 15-minute limit (target: no more than 12 minutes)? It runs the production worker path -- the same
`build_runner` / `DocumentJobExecutor` / PaddleOCR engine the Lambda handler uses -- against a real PostgreSQL
database, one queued upload job per invoice, and reports:

  * engine initialisation time (model load; paid once per Lambda cold start),
  * per-document wall-clock time, outcome and OCR provider,
  * peak resident memory of the process.

It exits non-zero if any document exceeds `--max-seconds` (default 720 = 12 minutes), if the engine falls back from
PaddleOCR, or if a job ends FAILED. Nothing is substituted: a PaddleOCR failure is a failure, never a Tesseract pass.

Run it inside the worker image under a CPU/memory ceiling that approximates the Lambda size under test (Lambda grants
roughly 1 vCPU per 1,769 MB: 4096 MB ~ 2.3 vCPU), e.g. in CI:

    docker run --cpus 2.3 --memory 4096m --network host --entrypoint python <worker-image> scripts/benchmark_worker.py

Environment: AP_AGENT_POSTGRES_DSN (runtime role), AP_AGENT_POSTGRES_MIGRATION_DSN (migrations + tenant registration),
AP_AGENT_REFERENCE_DATA_DIRECTORY. The database must be disposable. Uploads use a local scratch artifact root: the
Lambda S3 path only downloads the object first and is covered by the storage tests.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import resource
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0  # Linux: kilobytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixtures-dir", type=Path, default=Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "invoices")
    parser.add_argument("--max-seconds", type=float, default=720.0)
    parser.add_argument("--output", type=Path, default=None, help="Write the JSON summary here as well.")
    arguments = parser.parse_args()

    os.environ.setdefault("AP_AGENT_ENABLE_WORKER_EXECUTION", "true")
    os.environ.setdefault("AP_AGENT_WORKER_OCR_PROVIDER", "paddleocr")
    scratch = Path(tempfile.mkdtemp(prefix="ap-agent-benchmark-"))
    os.environ["AP_AGENT_ARTIFACT_ROOT"] = str(scratch / "artifacts")
    os.environ.setdefault("AP_AGENT_ENVIRONMENT", "test")
    os.environ.pop("AP_AGENT_ARTIFACT_STORAGE", None)  # local scratch store for the benchmark

    from ap_agent.artifacts.storage import LocalFilesystemArtifactStore
    from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy
    from ap_agent.db.connection import load_dsn
    from ap_agent.db.migration_runner import apply_all_migrations
    from ap_agent.db.roles import grant_schema_access
    from ap_agent.models.interface import InterfaceActor, InterfaceRole, interface_utc_now
    from ap_agent.models.operations import UploadLimits, WorkflowJobStatus
    from ap_agent.repositories.operations_repository import OperationsRepository
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository
    from ap_agent.services.operations_submission import submit_upload
    from ap_agent.worker.__main__ import build_runner
    from ap_agent.worker.config import load_worker_config

    memory_config = MemoryConfig(
        transport_policy=PostgresTransportPolicy(
            require_ssl=os.environ.get("AP_AGENT_BENCHMARK_REQUIRE_SSL", "false").lower() == "true"
        )
    )
    migration_dsn = load_dsn(memory_config, for_migration=True)
    apply_all_migrations(migration_dsn, memory_config)
    role = os.environ.get("AP_AGENT_POSTGRES_RUNTIME_ROLE", "").strip()

    if role:
        grant_schema_access(migration_dsn, memory_config, role_name=role)

    dsn = load_dsn(memory_config)
    tenant_id = uuid.uuid4()
    PostgresMemoryRepository(dsn, memory_config).register_tenant(
        tenant_id=tenant_id, tenant_key=f"benchmark-{tenant_id.hex[:8]}", display_name="Benchmark tenant"
    )

    config = load_worker_config()
    started = time.perf_counter()
    runner = build_runner(config, warm_up_ocr=True)  # raises OcrUnavailableError if PaddleOCR cannot be constructed
    init_seconds = time.perf_counter() - started
    print(f"engine initialised in {init_seconds:.1f}s (peak RSS {_peak_rss_mb():.0f} MB)", flush=True)

    operations = OperationsRepository(dsn, memory_config)
    store = LocalFilesystemArtifactStore(Path(os.environ["AP_AGENT_ARTIFACT_ROOT"]))
    actor = InterfaceActor(actor_id="benchmark", tenant_id=tenant_id, role=InterfaceRole.AP_OPERATOR, authenticated_at=interface_utc_now())
    documents = sorted(path for path in arguments.fixtures_dir.iterdir() if path.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg"})
    rows: list[dict] = []
    failed = False

    for path in documents:
        with path.open("rb") as stream:
            submission = submit_upload(
                repository=operations, store=store, limits=UploadLimits(), actor=actor, stream=stream,
                filename=path.name, declared_media_type=mimetypes.guess_type(path.name)[0],
                idempotency_key=f"benchmark-{uuid.uuid4().hex}",
            )

        job = operations.claim_job(submission.job.job_id, tenant_id=tenant_id)
        assert job is not None
        started = time.perf_counter()
        finished = runner.execute_claimed(job)
        elapsed = time.perf_counter() - started
        ok = finished.status is not WorkflowJobStatus.FAILED and elapsed <= arguments.max_seconds
        failed = failed or not ok
        rows.append(
            {
                "document": path.name,
                "bytes": path.stat().st_size,
                "seconds": round(elapsed, 1),
                "status": finished.status.value,
                "error_code": finished.error_code,
                "within_limit": elapsed <= arguments.max_seconds,
                "peak_rss_mb": round(_peak_rss_mb()),
            }
        )
        print(json.dumps(rows[-1]), flush=True)

    summary = {
        "provider": "paddleocr",
        "engine_init_seconds": round(init_seconds, 1),
        "max_seconds_allowed": arguments.max_seconds,
        "slowest_seconds": max((row["seconds"] for row in rows), default=0),
        "peak_rss_mb": round(_peak_rss_mb()),
        "cpu_count_visible": os.cpu_count(),
        "documents": rows,
        "passed": not failed and bool(rows),
    }
    print(json.dumps(summary, indent=2))

    if arguments.output:
        arguments.output.write_text(json.dumps(summary, indent=2))

    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

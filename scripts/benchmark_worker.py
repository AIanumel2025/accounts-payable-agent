#!/usr/bin/env python3
"""Representative worker benchmark with the REAL PaddleOCR provider (M11E.1).

Answers one question for the AWS deployment: can the OCR worker finish a representative invoice comfortably inside
the dispatcher's 15-minute bound (target: no more than 12 minutes) on the Fargate task's 4 vCPU / 8 GB? It runs the
production worker path -- the same claim -> fresh job process (`python -m ap_agent.worker.run_job`) ->
`DocumentJobExecutor` -> PaddleOCR engine that the Fargate task (`ap_agent.worker.dispatch_task`) uses -- against a real PostgreSQL database, one queued upload job per invoice, and reports:

  * per-document wall-clock time (INCLUDING the engine initialisation each job process pays), outcome and exit status,
  * the peak resident memory of each job process (the number that must fit the 8 GB task memory).

It exits non-zero if any document exceeds `--max-seconds` (default 720 = 12 minutes), if the engine falls back from
PaddleOCR, or if a job ends FAILED. Nothing is substituted: a PaddleOCR failure is a failure, never a Tesseract pass.

Run it inside the worker image under the Fargate task's CPU/memory (4 vCPU / 8192 MB), e.g. in CI:

    docker run --cpus 4 --memory 8192m --network host --entrypoint python <worker-image> scripts/benchmark_worker.py

Environment: AP_AGENT_POSTGRES_DSN (runtime role), AP_AGENT_POSTGRES_MIGRATION_DSN (migrations + tenant registration),
AP_AGENT_REFERENCE_DATA_DIRECTORY. The database must be disposable. Uploads use a local scratch artifact root: the
S3 path only downloads the object first and is covered by the storage tests.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixtures-dir", type=Path, default=Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "invoices")
    parser.add_argument("--max-seconds", type=float, default=720.0)
    parser.add_argument("--output", type=Path, default=None, help="Write the JSON summary here as well.")
    arguments = parser.parse_args()

    os.environ.setdefault("AP_AGENT_ENABLE_WORKER_EXECUTION", "true")  # inherited by every job process
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
    import subprocess

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
        child = subprocess.run(
            [sys.executable, "-m", "ap_agent.worker.run_job", str(job.job_id), str(tenant_id)],
            capture_output=True, text=True, timeout=arguments.max_seconds + 120, check=False,
        )
        elapsed = time.perf_counter() - started
        finished = operations.get_job(tenant_id, job.job_id)
        peak = next((int(line.split("=", 1)[1]) for line in child.stdout.splitlines() if line.startswith("RUN_JOB_PEAK_RSS_MB=")), None)
        ok = child.returncode == 0 and finished.status is not WorkflowJobStatus.FAILED and elapsed <= arguments.max_seconds
        failed = failed or not ok
        rows.append(
            {
                "document": path.name,
                "bytes": path.stat().st_size,
                "seconds": round(elapsed, 1),
                "status": finished.status.value,
                "error_code": finished.error_code,
                "within_limit": elapsed <= arguments.max_seconds,
                "exit_code": child.returncode,
                "job_process_peak_rss_mb": peak,
                **({"stderr_tail": child.stderr[-600:]} if child.returncode else {}),
            }
        )
        print(json.dumps(rows[-1]), flush=True)

    summary = {
        "provider": "paddleocr",
        "max_seconds_allowed": arguments.max_seconds,
        "slowest_seconds": max((row["seconds"] for row in rows), default=0),
        "peak_job_process_rss_mb": max((row["job_process_peak_rss_mb"] or 0 for row in rows), default=0),
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

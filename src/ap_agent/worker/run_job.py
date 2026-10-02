"""`python -m ap_agent.worker.run_job <job-id> <tenant-id>` -- execute ONE already-claimed job in a fresh process (M11E.1).

The Lambda worker handler runs every job in a child process started from this module. PaddleOCR and its rendering
libraries do not return memory between documents (the resident set grows with each job), and a warm Lambda container
serves many jobs; a fresh process per job gives each invoice the full memory budget, releases everything when it ends,
and turns an out-of-memory kill or a timeout into a failure the *parent* can record (the Lambda environment survives).

The job must be `RUNNING` (claimed by the parent, `OperationsRepository.claim_job`) and belong to the stated tenant.
The terminal state (completed, review required or failed) is written to PostgreSQL by the same executors the polling
worker uses; this process only reports its exit code and, on a line of its own, its peak resident memory.

Exit codes: 0 the job was executed (its outcome is in the database), 4 configuration, 5 OCR unavailable,
6 the job is not runnable (missing, wrong tenant, not RUNNING).
"""

from __future__ import annotations

import json
import logging
import resource
import sys
from uuid import UUID

EXIT_OK = 0
EXIT_CONFIGURATION = 4
EXIT_OCR_UNAVAILABLE = 5
EXIT_NOT_RUNNABLE = 6

PEAK_MEMORY_PREFIX = "RUN_JOB_PEAK_RSS_MB="


def _peak_rss_mb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)  # Linux reports kilobytes


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    try:
        job_id, tenant_id = UUID(arguments[0]), UUID(arguments[1])
    except (IndexError, ValueError):
        print("run_job: usage: run_job <job-id> <tenant-id>", file=sys.stderr)
        return EXIT_NOT_RUNNABLE

    from ap_agent.aws.paddle_cache import prepare_paddlex_cache
    from ap_agent.config.deployment import HostedConfigurationError
    from ap_agent.models.operations import WorkflowJobStatus
    from ap_agent.worker.__main__ import OcrUnavailableError, build_runner
    from ap_agent.worker.config import WorkerConfigurationError, load_worker_config

    try:
        prepare_paddlex_cache()  # before PaddleOCR is imported
        runner = build_runner(load_worker_config(), warm_up_ocr=True)
    except (WorkerConfigurationError, HostedConfigurationError) as error:
        print(f"run_job: configuration error ({type(error).__name__}).", file=sys.stderr)
        return EXIT_CONFIGURATION
    except OcrUnavailableError as error:
        print(f"run_job: the OCR provider could not be initialised ({error}).", file=sys.stderr)
        return EXIT_OCR_UNAVAILABLE

    job = runner.operations.peek_job(job_id)

    if job is None or job.tenant_id != tenant_id or job.status is not WorkflowJobStatus.RUNNING:
        print("run_job: the job is not runnable.", file=sys.stderr)
        return EXIT_NOT_RUNNABLE

    finished = runner.execute_claimed(job)
    print(f"run_job: job finished as {finished.status.value}.", flush=True)
    print(f"{PEAK_MEMORY_PREFIX}{_peak_rss_mb()}", flush=True)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

"""`python -m ap_agent.worker.dispatch_task` -- run ONE queued job, then exit (M11E.2).

This is the entry point of the on-demand ECS Fargate OCR task (`deploy/aws/template.yaml`, `OcrTaskDefinition`). The
dispatcher Lambda (`ap_agent.aws.fargate_dispatcher`) starts exactly one task per SQS message and passes the message body
in `AP_AGENT_DISPATCH_MESSAGE` and the delivery attempt in `AP_AGENT_DISPATCH_RECEIVE_COUNT` (job id, tenant id and
generation only -- no secret). Database and application secrets are read from SSM Parameter Store by this process, using
the task role. The message is handled by exactly the same logic as the other workers
(`ap_agent.worker.lambda_handler.process_dispatch_record`): the job is re-read from PostgreSQL, claimed atomically only
while `QUEUED` (a redelivery is a no-op), executed through the production executors, and an orphaned `RUNNING` job on a
redelivery is failed rather than silently re-run.

The task never loops and never polls: no always-on process. The container's exit code is the contract with the dispatcher
(which acknowledges the queue message only for exit code 0):

    0  handled -- the job was processed (whatever its recorded outcome) or the message was a harmless duplicate
    4  configuration error        5  OCR provider unavailable      6  unusable (malformed/unknown) message
    7  transient failure          8  hard deadline reached (AP_AGENT_TASK_MAX_SECONDS)

A job that ends FAILED/REVIEW_REQUIRED is a *handled* outcome (exit 0): it is recorded in PostgreSQL and a person acts on it.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any, Callable, Mapping, Optional

EXIT_OK = 0
EXIT_CONFIGURATION = 4
EXIT_OCR_UNAVAILABLE = 5
EXIT_POISON_MESSAGE = 6
EXIT_TRANSIENT = 7
EXIT_DEADLINE = 8

MESSAGE_ENVIRONMENT_VARIABLE = "AP_AGENT_DISPATCH_MESSAGE"
RECEIVE_COUNT_ENVIRONMENT_VARIABLE = "AP_AGENT_DISPATCH_RECEIVE_COUNT"
MAX_SECONDS_ENVIRONMENT_VARIABLE = "AP_AGENT_TASK_MAX_SECONDS"


def _receive_count(env: Mapping[str, str]) -> int:
    try:
        return max(1, int(env.get(RECEIVE_COUNT_ENVIRONMENT_VARIABLE, "1")))
    except ValueError:
        return 1


def _start_deadline(env: Mapping[str, str]) -> Optional[threading.Timer]:
    """A self-imposed hard deadline: if the job outlives it the process exits 8 (the job stays RUNNING and the
    redelivered message fails it as interrupted). Disabled unless AP_AGENT_TASK_MAX_SECONDS is a positive number."""

    try:
        seconds = float(env.get(MAX_SECONDS_ENVIRONMENT_VARIABLE, "0"))
    except ValueError:
        return None

    if seconds <= 0:
        return None

    def expire() -> None:
        print("worker task: the hard deadline was reached; exiting.", file=sys.stderr, flush=True)
        os._exit(EXIT_DEADLINE)

    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    timer.start()
    return timer


def main(
    environment: Optional[Mapping[str, str]] = None,
    *,
    runner_factory: Optional[Callable[[], Any]] = None,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    env = os.environ if environment is None else environment

    from ap_agent.services.job_dispatch import DispatchMessage, DispatchMessageError

    body = env.get(MESSAGE_ENVIRONMENT_VARIABLE, "")

    # Validate the message BEFORE the expensive start-up (secrets, database, OCR engine): a malformed message costs nothing.
    try:
        DispatchMessage.parse(body)
    except DispatchMessageError as error:
        print(f"worker task: unusable dispatch message ({error}).", file=sys.stderr)
        return EXIT_POISON_MESSAGE

    from ap_agent.aws.paddle_cache import prepare_paddlex_cache
    from ap_agent.aws.secrets import SecretLoadError, load_ssm_parameters_into_environment
    from ap_agent.config.deployment import HostedConfigurationError
    from ap_agent.worker.__main__ import OcrUnavailableError, build_runner
    from ap_agent.worker.config import WorkerConfigurationError, load_worker_config
    from ap_agent.worker.lambda_handler import MessageOutcome, PoisonMessageError, process_dispatch_record

    deadline = _start_deadline(env)

    try:
        prepare_paddlex_cache()  # no-op unless the image bakes its OCR assets (then: models and fonts come from the image)

        if runner_factory is None:
            load_ssm_parameters_into_environment()
            runner = build_runner(load_worker_config(), warm_up_ocr=True)
        else:
            runner = runner_factory()
    except (SecretLoadError, WorkerConfigurationError, HostedConfigurationError) as error:
        print(f"worker task: configuration error ({type(error).__name__}).", file=sys.stderr)
        return EXIT_CONFIGURATION
    except OcrUnavailableError as error:
        print(f"worker task: the OCR provider could not be initialised ({error}).", file=sys.stderr)
        return EXIT_OCR_UNAVAILABLE

    try:
        outcome: MessageOutcome = process_dispatch_record(
            {"body": body, "attributes": {"ApproximateReceiveCount": str(_receive_count(env))}}, runner=runner
        )
    except PoisonMessageError as error:
        print(f"worker task: unusable dispatch message ({error}).", file=sys.stderr)
        return EXIT_POISON_MESSAGE
    except Exception as error:  # noqa: BLE001
        print(f"worker task: transient failure ({type(error).__name__}).", file=sys.stderr)
        return EXIT_TRANSIENT
    finally:
        if deadline is not None:
            deadline.cancel()

    print(f"worker task: {outcome.value}.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

"""Queue-to-Fargate dispatcher (M11E.2): a small Lambda that runs ONE heavy OCR job as ONE on-demand ECS Fargate task.

    SQS FIFO (one message group, batch size 1) -> this Lambda -> ecs:RunTask -> wait for STOPPED -> acknowledge

Why: the AWS account caps Lambda functions at 3,008 MB, while the verified PaddleOCR benchmark peaks near 6.6 GB
(docs/m11e1_aws_go_live_report.md). The OCR therefore runs in a Fargate task (4 vCPU / 8 GB) started only when a job
exists; this function stays tiny, imports no OCR library and holds no application secret.

**Task completion, not task creation, is the success boundary.** The function returns (and so lets Lambda delete the
SQS message) only after the task has reached `STOPPED` and its essential container exited with code 0. Everything else
raises, so SQS retry and the dead-letter queue stay in force:

    RunTask error / placement failure            -> FargateStartError
    DescribeTasks failing repeatedly             -> FargateWaitError   (task is stopped on a best-effort basis)
    container exit code != 0 / missing           -> FargateTaskFailedError
    approaching the Lambda deadline              -> FargateTimeoutError (the task is stopped first)
    malformed message / wrong batch / group      -> InvalidDispatchError

Because the function waits inside the invocation and the queue is FIFO with a single message group and batch size 1,
there is never more than one active task. The job's own `QUEUED -> RUNNING` database claim (done inside the task) remains
the duplicate-execution guard: a redelivered message starts a task that finds nothing to claim and exits 0.

Before raising, the function shortens the message's visibility timeout (best effort) so a failed message is retried after
minutes rather than after the queue's long crash-recovery timeout (5400 s) -- in a one-group FIFO queue a stuck message
blocks every later job.

Nothing here logs a secret: only job and task identifiers, status codes and exit codes. `boto3` is imported lazily.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Mapping, Optional

from ap_agent.services.job_dispatch import MESSAGE_GROUP_ID, DispatchMessage, DispatchMessageError

__all__ = [
    "CONTAINER_NAME",
    "MESSAGE_ENVIRONMENT_VARIABLE",
    "RECEIVE_COUNT_ENVIRONMENT_VARIABLE",
    "FargateDispatchError",
    "InvalidDispatchError",
    "FargateStartError",
    "FargateWaitError",
    "FargateTaskFailedError",
    "FargateTimeoutError",
    "DispatcherConfig",
    "dispatch_record",
    "handler",
]

_LOGGER = logging.getLogger("ap_agent.fargate_dispatcher")

CONTAINER_NAME = "worker"
MESSAGE_ENVIRONMENT_VARIABLE = "AP_AGENT_DISPATCH_MESSAGE"
RECEIVE_COUNT_ENVIRONMENT_VARIABLE = "AP_AGENT_DISPATCH_RECEIVE_COUNT"

_SAFE_REASON = re.compile(r"[^A-Za-z0-9_.:\- ]")


class FargateDispatchError(RuntimeError):
    """Base class. `code` is a stable, secret-free identifier."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InvalidDispatchError(FargateDispatchError):
    pass


class FargateStartError(FargateDispatchError):
    pass


class FargateWaitError(FargateDispatchError):
    pass


class FargateTaskFailedError(FargateDispatchError):
    pass


class FargateTimeoutError(FargateDispatchError):
    pass


@dataclass(frozen=True)
class DispatcherConfig:
    cluster_arn: str
    task_definition_arn: str
    subnet_ids: tuple[str, ...]
    security_group_ids: tuple[str, ...]
    queue_url: str = ""
    container_name: str = CONTAINER_NAME
    poll_interval_seconds: float = 10.0
    # Remaining Lambda time at which the task is stopped and the invocation fails safely.
    safety_margin_seconds: float = 60.0
    # Consecutive DescribeTasks failures tolerated before giving up.
    maximum_describe_failures: int = 3
    # Visibility timeout (seconds) applied to a failed message, by receive count (1st, 2nd, ... failure).
    retry_visibility_seconds: tuple[int, ...] = (60, 300, 900)

    @classmethod
    def from_environment(cls, env: Optional[Mapping[str, str]] = None) -> "DispatcherConfig":
        source = os.environ if env is None else env

        def need(name: str) -> str:
            value = source.get(name, "").strip()

            if not value:
                raise InvalidDispatchError(f"CONFIGURATION_MISSING:{name}")

            return value

        return cls(
            cluster_arn=need("OCR_CLUSTER_ARN"),
            task_definition_arn=need("OCR_TASK_DEFINITION_ARN"),
            subnet_ids=tuple(item.strip() for item in need("OCR_SUBNET_IDS").split(",") if item.strip()),
            security_group_ids=tuple(item.strip() for item in need("OCR_SECURITY_GROUP_IDS").split(",") if item.strip()),
            queue_url=source.get("JOBS_QUEUE_URL", "").strip(),
        )


def _safe(text: Any) -> str:
    return _SAFE_REASON.sub("", str(text))[:120]


def _receive_count(record: Mapping[str, Any]) -> int:
    try:
        return max(1, int((record.get("attributes") or {}).get("ApproximateReceiveCount", "1")))
    except (TypeError, ValueError):
        return 1


def _validate_record(record: Mapping[str, Any]) -> DispatchMessage:
    try:
        message = DispatchMessage.parse(record.get("body"))
    except DispatchMessageError as error:
        raise InvalidDispatchError(f"MESSAGE_INVALID:{error}") from error

    group = (record.get("attributes") or {}).get("MessageGroupId")

    if group is not None and group != MESSAGE_GROUP_ID:
        raise InvalidDispatchError("MESSAGE_GROUP_INVALID")

    return message


def _start_task(ecs: Any, config: DispatcherConfig, record: Mapping[str, Any]) -> str:
    try:
        response = ecs.run_task(
            cluster=config.cluster_arn,
            taskDefinition=config.task_definition_arn,
            launchType="FARGATE",
            count=1,
            startedBy="ap-agent-dispatcher",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": list(config.subnet_ids),
                    "securityGroups": list(config.security_group_ids),
                    "assignPublicIp": "ENABLED",  # outbound-only (no inbound rule): the NAT-free way to reach Neon/S3/ECR
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": config.container_name,
                        # Job id, tenant id and generation only: no secret ever travels in the override.
                        "environment": [
                            {"name": MESSAGE_ENVIRONMENT_VARIABLE, "value": str(record.get("body"))},
                            {"name": RECEIVE_COUNT_ENVIRONMENT_VARIABLE, "value": str(_receive_count(record))},
                        ],
                    }
                ]
            },
        )
    except Exception as error:  # noqa: BLE001 - classify, never leak the message
        _LOGGER.error("RunTask failed (%s).", type(error).__name__)
        raise FargateStartError(f"RUN_TASK_FAILED:{type(error).__name__}") from error

    failures = response.get("failures") or []
    tasks = response.get("tasks") or []

    if failures or len(tasks) != 1 or not tasks[0].get("taskArn"):
        reason = _safe(failures[0].get("reason", "NO_TASK_STARTED")) if failures else "NO_TASK_STARTED"
        _LOGGER.error("RunTask did not start exactly one task (%s).", reason)
        raise FargateStartError(f"PLACEMENT_FAILED:{reason}")

    return str(tasks[0]["taskArn"])


def _stop_task(ecs: Any, config: DispatcherConfig, task_arn: str, reason: str) -> None:
    try:
        ecs.stop_task(cluster=config.cluster_arn, task=task_arn, reason=reason[:120])
    except Exception as error:  # noqa: BLE001 - best effort
        _LOGGER.error("StopTask failed (%s).", type(error).__name__)


def _evaluate_stopped(task: Mapping[str, Any], config: DispatcherConfig) -> None:
    container = next((item for item in task.get("containers") or [] if item.get("name") == config.container_name), None)
    exit_code = None if container is None else container.get("exitCode")

    if exit_code is None:
        # Never started (image pull, placement), killed, or reported nothing: not a success.
        raise FargateTaskFailedError(f"NO_EXIT_CODE:{_safe(task.get('stopCode', ''))}:{_safe(task.get('stoppedReason', ''))}")

    if exit_code != 0:
        raise FargateTaskFailedError(f"EXIT_CODE_{exit_code}")


def _wait_for_task(
    ecs: Any,
    config: DispatcherConfig,
    task_arn: str,
    *,
    remaining_seconds: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    failures = 0

    while True:
        if remaining_seconds() < config.safety_margin_seconds:
            _stop_task(ecs, config, task_arn, "Dispatcher deadline reached")
            raise FargateTimeoutError("DISPATCHER_DEADLINE")

        try:
            response = ecs.describe_tasks(cluster=config.cluster_arn, tasks=[task_arn])
            tasks = response.get("tasks") or []

            if len(tasks) != 1:
                raise LookupError("task not reported")

            failures = 0
        except Exception as error:  # noqa: BLE001 - transient until proven otherwise
            failures += 1
            _LOGGER.error("DescribeTasks failed (%s), attempt %s.", type(error).__name__, failures)

            if failures >= config.maximum_describe_failures:
                _stop_task(ecs, config, task_arn, "Dispatcher lost track of the task")
                raise FargateWaitError(f"DESCRIBE_FAILED:{type(error).__name__}") from error

            sleep(config.poll_interval_seconds)
            continue

        task = tasks[0]

        if task.get("lastStatus") == "STOPPED":
            _evaluate_stopped(task, config)
            return

        sleep(config.poll_interval_seconds)


def _shorten_visibility(sqs: Any, config: DispatcherConfig, record: Mapping[str, Any]) -> None:
    receipt = record.get("receiptHandle")

    if sqs is None or not config.queue_url or not receipt:
        return

    steps = config.retry_visibility_seconds
    seconds = steps[min(_receive_count(record), len(steps)) - 1]

    try:
        sqs.change_message_visibility(QueueUrl=config.queue_url, ReceiptHandle=receipt, VisibilityTimeout=seconds)
    except Exception as error:  # noqa: BLE001 - best effort
        _LOGGER.error("ChangeMessageVisibility failed (%s).", type(error).__name__)


def dispatch_record(
    record: Mapping[str, Any],
    *,
    ecs: Any,
    config: DispatcherConfig,
    remaining_seconds: Callable[[], float],
    sleep: Callable[[float], None] = time.sleep,
    sqs: Any = None,
) -> str:
    """Run one job to completion on Fargate. Returns the task ARN on success; raises `FargateDispatchError` otherwise.

    Returning is the *only* success path (the Lambda then acknowledges the message). Every failure -- including an
    invalid message -- raises after (best effort) shortening the message's visibility timeout.
    """

    task_arn: Optional[str] = None

    try:
        message = _validate_record(record)
        task_arn = _start_task(ecs, config, record)
        _LOGGER.info("Job %s: started Fargate task.", message.job_id)
        _wait_for_task(ecs, config, task_arn, remaining_seconds=remaining_seconds, sleep=sleep)
        _LOGGER.info("Job %s: Fargate task completed successfully.", message.job_id)
        return task_arn
    except FargateDispatchError as error:
        _LOGGER.error("Dispatch failed (%s).", error.code)
        _shorten_visibility(sqs, config, record)
        raise


@lru_cache(maxsize=1)
def _clients() -> tuple[Any, Any]:
    import boto3
    from botocore.config import Config

    configuration = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 3, "mode": "standard"})
    return boto3.client("ecs", config=configuration), boto3.client("sqs", config=configuration)


def handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    logging.getLogger().setLevel(logging.INFO)
    records = event.get("Records") or []

    if len(records) != 1:  # the event-source mapping is configured with batch size 1; anything else is a misconfiguration
        raise InvalidDispatchError("BATCH_SIZE_INVALID")

    config = DispatcherConfig.from_environment()
    ecs, sqs = _clients()
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    remaining_seconds = (lambda: remaining() / 1000.0) if callable(remaining) else (lambda: float("inf"))

    task_arn = dispatch_record(records[0], ecs=ecs, config=config, remaining_seconds=remaining_seconds, sqs=sqs)
    return {"status": "COMPLETED", "task": task_arn.rsplit("/", 1)[-1]}

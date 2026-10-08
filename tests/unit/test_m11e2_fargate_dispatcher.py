"""M11E.2: the queue-to-Fargate dispatcher (mocked ECS/SQS) and the Fargate task entry point. No AWS, no database, no OCR."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from typing import Any

import pytest

from ap_agent.aws import fargate_dispatcher as fd
from ap_agent.aws.fargate_dispatcher import (
    DispatcherConfig,
    FargateStartError,
    FargateTaskFailedError,
    FargateTimeoutError,
    FargateWaitError,
    InvalidDispatchError,
    dispatch_record,
)
from ap_agent.models.operations import WorkflowJobStatus
from ap_agent.services.job_dispatch import MESSAGE_GROUP_ID, DispatchMessage
from ap_agent.worker import dispatch_task
from tests.unit.test_m11e1_queue_worker import FakeRunner, _job

pytestmark = pytest.mark.unit

TASK = "arn:aws:ecs:eu-west-2:000000000000:task/ap-agent-production/abc123"
QUEUE = "https://sqs.eu-west-2.amazonaws.com/000000000000/ap-agent-production-jobs.fifo"


def _config(**overrides) -> DispatcherConfig:
    values = dict(
        cluster_arn="arn:aws:ecs:eu-west-2:000000000000:cluster/ap-agent-production",
        task_definition_arn="arn:aws:ecs:eu-west-2:000000000000:task-definition/ap-agent-production-ocr:3",
        subnet_ids=("subnet-a", "subnet-b"), security_group_ids=("sg-1",), queue_url=QUEUE, poll_interval_seconds=0.0,
    )
    values.update(overrides)
    return DispatcherConfig(**values)


def _record(*, receive_count=1, body=None, group=MESSAGE_GROUP_ID):
    job_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    return {
        "messageId": "m-1",
        "receiptHandle": "receipt-1",
        "body": body if body is not None else DispatchMessage(job_id, tenant_id).to_body(),
        "attributes": {"ApproximateReceiveCount": str(receive_count), "MessageGroupId": group},
    }


def _stopped(exit_code=0, *, name="worker", reason="Essential container in task exited", code="EssentialContainerExited"):
    container = {"name": name}
    if exit_code is not None:
        container["exitCode"] = exit_code
    return {"tasks": [{"taskArn": TASK, "lastStatus": "STOPPED", "stopCode": code, "stoppedReason": reason, "containers": [container]}]}


def _running():
    return {"tasks": [{"taskArn": TASK, "lastStatus": "RUNNING", "containers": [{"name": "worker"}]}]}


class FakeEcs:
    def __init__(self, describes, *, run=None, run_error=None):
        self.describes = list(describes)
        self.run = run if run is not None else {"tasks": [{"taskArn": TASK}], "failures": []}
        self.run_error = run_error
        self.run_calls: list[dict[str, Any]] = []
        self.describe_calls = 0
        self.stops: list[dict[str, Any]] = []
        self.events: list[str] = []

    def run_task(self, **kwargs):
        self.events.append("run")
        self.run_calls.append(kwargs)
        if self.run_error:
            raise self.run_error
        return self.run

    def describe_tasks(self, **kwargs):
        self.events.append("describe")
        self.describe_calls += 1
        item = self.describes.pop(0) if len(self.describes) > 1 else self.describes[0]
        if isinstance(item, Exception):
            raise item
        return item

    def stop_task(self, **kwargs):
        self.events.append("stop")
        self.stops.append(kwargs)
        return {}


class FakeSqs:
    def __init__(self, fail=False):
        self.visibility: list[dict[str, Any]] = []
        self.fail = fail
        self.deleted = 0

    def change_message_visibility(self, **kwargs):
        if self.fail:
            raise ConnectionError("down")
        self.visibility.append(kwargs)

    def delete_message(self, **kwargs):  # the dispatcher must never delete: Lambda does, on success only
        self.deleted += 1


def _run(ecs, record=None, *, sqs=None, remaining=lambda: 10_000.0, config=None):
    return dispatch_record(record or _record(), ecs=ecs, config=config or _config(), remaining_seconds=remaining, sleep=lambda s: None, sqs=sqs)


# -- success boundary -------------------------------------------------------------------------


def test_success_starts_exactly_one_task_waits_for_stopped_and_returns_the_arn():
    ecs = FakeEcs([_running(), _running(), _stopped(0)])
    sqs = FakeSqs()

    assert _run(ecs, sqs=sqs) == TASK
    assert len(ecs.run_calls) == 1 and ecs.describe_calls == 3 and ecs.stops == []
    assert sqs.visibility == [] and sqs.deleted == 0  # no back-off on success; the dispatcher never deletes the message


def test_the_task_request_is_fargate_one_task_outbound_only_and_carries_no_secret():
    ecs = FakeEcs([_stopped(0)])
    record = _record(receive_count=2)
    _run(ecs, record)
    call = ecs.run_calls[0]

    assert call["launchType"] == "FARGATE" and call["count"] == 1
    assert call["cluster"].endswith("cluster/ap-agent-production") and call["taskDefinition"].endswith("ap-agent-production-ocr:3")
    network = call["networkConfiguration"]["awsvpcConfiguration"]
    assert network == {"subnets": ["subnet-a", "subnet-b"], "securityGroups": ["sg-1"], "assignPublicIp": "ENABLED"}
    override = call["overrides"]["containerOverrides"]
    assert [item["name"] for item in override] == ["worker"]
    environment = {item["name"]: item["value"] for item in override[0]["environment"]}
    assert environment == {fd.MESSAGE_ENVIRONMENT_VARIABLE: record["body"], fd.RECEIVE_COUNT_ENVIRONMENT_VARIABLE: "2"}
    assert set(json.loads(record["body"])) == {"v", "job_id", "tenant_id", "dispatch_generation"}
    # Nothing but the dispatch message may be overridden: no command, no memory/cpu, no role.
    assert set(override[0]) == {"name", "environment"} and set(call["overrides"]) == {"containerOverrides"}


def test_the_message_is_not_acknowledged_before_the_task_has_stopped():
    ecs = FakeEcs([_running(), _running(), _running(), _stopped(0)])
    _run(ecs)

    assert ecs.events == ["run", "describe", "describe", "describe", "describe"]  # every describe happened before returning


# -- failures raise (so SQS keeps the message and the DLQ stays in force) -------------------------


@pytest.mark.parametrize("exit_code", [1, 4, 5, 6, 7, 8, 137])
def test_a_non_zero_exit_code_raises_and_backs_off(exit_code):
    sqs = FakeSqs()

    with pytest.raises(FargateTaskFailedError) as raised:
        _run(FakeEcs([_stopped(exit_code)]), sqs=sqs)

    assert raised.value.code == f"EXIT_CODE_{exit_code}"
    assert sqs.visibility == [{"QueueUrl": QUEUE, "ReceiptHandle": "receipt-1", "VisibilityTimeout": 60}]


def test_a_stopped_task_without_an_exit_code_is_a_failure():
    # e.g. the image could not be pulled, or the task was stopped before the container started.
    with pytest.raises(FargateTaskFailedError) as raised:
        _run(FakeEcs([_stopped(None, reason="CannotPullContainerError: pull access denied", code="TaskFailedToStart")]))

    assert raised.value.code.startswith("NO_EXIT_CODE:TaskFailedToStart")


def test_an_exit_code_of_another_container_does_not_count():
    with pytest.raises(FargateTaskFailedError):
        _run(FakeEcs([_stopped(0, name="sidecar")]))


def test_run_task_exception_is_a_start_failure_without_leaking_details():
    sqs = FakeSqs()

    with pytest.raises(FargateStartError) as raised:
        _run(FakeEcs([], run_error=PermissionError("secret-account-detail")), sqs=sqs)

    assert raised.value.code == "RUN_TASK_FAILED:PermissionError" and "secret" not in str(raised.value)
    assert sqs.visibility


@pytest.mark.parametrize(
    "response",
    [
        {"tasks": [], "failures": [{"arn": "x", "reason": "RESOURCE:CPU"}]},
        {"tasks": [{"taskArn": TASK}], "failures": [{"reason": "MISSING"}]},
        {"tasks": [], "failures": []},
        {"tasks": [{"taskArn": TASK}, {"taskArn": TASK + "2"}], "failures": []},
        {"tasks": [{}], "failures": []},
    ],
)
def test_a_placement_failure_never_waits(response):
    ecs = FakeEcs([_stopped(0)], run=response)

    with pytest.raises(FargateStartError):
        _run(ecs)

    assert ecs.describe_calls == 0


def test_a_transient_describe_failure_is_retried():
    ecs = FakeEcs([ConnectionError("blip"), _running(), ConnectionError("blip"), _stopped(0)])

    assert _run(ecs) == TASK


def test_repeated_describe_failures_stop_the_task_and_fail():
    ecs = FakeEcs([ConnectionError("down")])

    with pytest.raises(FargateWaitError) as raised:
        _run(ecs, sqs=FakeSqs())

    assert raised.value.code == "DESCRIBE_FAILED:ConnectionError" and len(ecs.stops) == 1 and ecs.describe_calls == 3


def test_an_unknown_task_counts_as_a_describe_failure():
    with pytest.raises(FargateWaitError):
        _run(FakeEcs([{"tasks": []}]))


def test_the_task_is_stopped_before_the_lambda_deadline():
    clock = {"left": 400.0}
    ecs = FakeEcs([_running()])

    def remaining():
        clock["left"] -= 100.0
        return clock["left"]

    sqs = FakeSqs()

    with pytest.raises(FargateTimeoutError) as raised:
        _run(ecs, sqs=sqs, remaining=remaining, config=_config(safety_margin_seconds=60.0))

    assert raised.value.code == "DISPATCHER_DEADLINE" and len(ecs.stops) == 1
    assert ecs.stops[0]["task"] == TASK and ecs.stops[0]["cluster"].endswith("ap-agent-production")
    assert sqs.visibility  # back-off, so the retry is not blocked for 90 minutes


def test_a_failing_stop_task_does_not_mask_the_timeout():
    ecs = FakeEcs([_running()])
    ecs.stop_task = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("denied"))  # type: ignore[method-assign]

    with pytest.raises(FargateTimeoutError):
        _run(ecs, remaining=lambda: 1.0)


# -- malformed input ------------------------------------------------------------------------------


@pytest.mark.parametrize("body", [None, "", "not json", "{}", json.dumps({"v": 1, "job_id": "nope", "tenant_id": "x", "dispatch_generation": 1})])
def test_a_malformed_message_never_starts_a_task(body):
    ecs = FakeEcs([_stopped(0)])
    sqs = FakeSqs()

    with pytest.raises(InvalidDispatchError) as raised:
        _run(ecs, _record(body=body if body is not None else None) | {"body": body}, sqs=sqs)

    assert ecs.run_calls == [] and raised.value.code.startswith("MESSAGE_INVALID")
    assert sqs.visibility  # still backs off: the redrive policy then moves it to the DLQ after three receives


def test_a_message_of_the_wrong_group_never_starts_a_task():
    ecs = FakeEcs([_stopped(0)])

    with pytest.raises(InvalidDispatchError, match="MESSAGE_GROUP_INVALID"):
        _run(ecs, _record(group="another-group"))

    assert ecs.run_calls == []


# -- visibility back-off ----------------------------------------------------------------------------


@pytest.mark.parametrize("receive_count,seconds", [(1, 60), (2, 300), (3, 900), (9, 900)])
def test_the_visibility_back_off_grows_with_the_receive_count(receive_count, seconds):
    sqs = FakeSqs()

    with pytest.raises(FargateTaskFailedError):
        _run(FakeEcs([_stopped(1)]), _record(receive_count=receive_count), sqs=sqs)

    assert sqs.visibility[0]["VisibilityTimeout"] == seconds


def test_a_failing_back_off_does_not_hide_the_real_error():
    with pytest.raises(FargateTaskFailedError):
        _run(FakeEcs([_stopped(1)]), sqs=FakeSqs(fail=True))


def test_no_back_off_without_a_queue_url_or_client():
    sqs = FakeSqs()

    with pytest.raises(FargateTaskFailedError):
        _run(FakeEcs([_stopped(1)]), sqs=sqs, config=_config(queue_url=""))

    with pytest.raises(FargateTaskFailedError):
        _run(FakeEcs([_stopped(1)]), sqs=None)

    assert sqs.visibility == []


# -- Lambda handler -------------------------------------------------------------------------------------


class _Context:
    def get_remaining_time_in_millis(self):
        return 900_000


def _handler_environment(monkeypatch):
    for name, value in {
        "OCR_CLUSTER_ARN": "arn:aws:ecs:eu-west-2:000000000000:cluster/c", "OCR_TASK_DEFINITION_ARN": "arn:td",
        "OCR_SUBNET_IDS": "subnet-a, subnet-b", "OCR_SECURITY_GROUP_IDS": "sg-1", "JOBS_QUEUE_URL": QUEUE,
    }.items():
        monkeypatch.setenv(name, value)


def test_the_handler_returns_only_after_success_and_raises_otherwise(monkeypatch):
    _handler_environment(monkeypatch)
    ecs = FakeEcs([_stopped(0)])
    monkeypatch.setattr(fd, "_clients", lambda: (ecs, FakeSqs()))
    monkeypatch.setattr(fd.time, "sleep", lambda s: None)

    assert fd.handler({"Records": [_record()]}, _Context()) == {"status": "COMPLETED", "task": "abc123"}
    assert ecs.run_calls[0]["networkConfiguration"]["awsvpcConfiguration"]["subnets"] == ["subnet-a", "subnet-b"]

    failing = FakeEcs([_stopped(1)])
    monkeypatch.setattr(fd, "_clients", lambda: (failing, FakeSqs()))

    with pytest.raises(FargateTaskFailedError):
        fd.handler({"Records": [_record()]}, _Context())


@pytest.mark.parametrize("records", [[], [_record(), _record()]])
def test_the_handler_refuses_any_batch_other_than_one(monkeypatch, records):
    _handler_environment(monkeypatch)
    ecs = FakeEcs([_stopped(0)])
    monkeypatch.setattr(fd, "_clients", lambda: (ecs, FakeSqs()))

    with pytest.raises(InvalidDispatchError, match="BATCH_SIZE_INVALID"):
        fd.handler({"Records": records}, _Context())

    assert ecs.run_calls == []


def test_missing_configuration_is_reported_by_name_only(monkeypatch):
    monkeypatch.delenv("OCR_CLUSTER_ARN", raising=False)
    _handler_environment(monkeypatch)
    monkeypatch.delenv("OCR_CLUSTER_ARN")

    with pytest.raises(InvalidDispatchError, match="CONFIGURATION_MISSING:OCR_CLUSTER_ARN"):
        DispatcherConfig.from_environment()


def test_the_dispatcher_imports_no_ocr_or_heavy_library():
    code = (
        "import sys, ap_agent.aws.fargate_dispatcher;"
        "bad=[m for m in ('paddle','paddleocr','pytesseract','fitz','cv2','numpy','PIL','pandas','boto3','psycopg') if m in sys.modules];"
        "print(bad)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert result.stdout.strip() == "[]"


# -- the Fargate task entry point (dispatch_task) -------------------------------------------------------------


def _environment(job, *, receive_count="1", body=None, **extra):
    return {
        dispatch_task.MESSAGE_ENVIRONMENT_VARIABLE: body if body is not None else DispatchMessage(job.job_id, job.tenant_id).to_body(),
        dispatch_task.RECEIVE_COUNT_ENVIRONMENT_VARIABLE: receive_count,
        **extra,
    }


def test_the_task_processes_the_job_once_and_exits_zero():
    job = _job()
    runner = FakeRunner(job)

    assert dispatch_task.main(_environment(job), runner_factory=lambda: runner) == 0
    assert runner.executions == [job.job_id]


def test_a_failed_or_review_outcome_is_still_a_handled_exit_zero():
    job = _job()
    runner = FakeRunner(job, outcome=WorkflowJobStatus.FAILED)

    assert dispatch_task.main(_environment(job), runner_factory=lambda: runner) == 0


def test_a_repeated_message_never_executes_twice_and_exits_zero():
    job = _job()
    runner = FakeRunner(job)

    assert dispatch_task.main(_environment(job), runner_factory=lambda: runner) == 0
    assert dispatch_task.main(_environment(job, receive_count="2"), runner_factory=lambda: runner) == 0
    assert dispatch_task.main(_environment(job, receive_count="3"), runner_factory=lambda: runner) == 0
    assert runner.executions == [job.job_id]  # the QUEUED -> RUNNING claim is the duplicate-execution guard


def test_an_orphaned_running_job_is_failed_on_redelivery_not_rerun():
    from dataclasses import replace

    job = replace(_job(), status=WorkflowJobStatus.RUNNING)
    runner = FakeRunner(job)

    assert dispatch_task.main(_environment(job, receive_count="2"), runner_factory=lambda: runner) == 0
    assert runner.operations.job.status is WorkflowJobStatus.FAILED and runner.executions == []


def test_the_receive_count_reaches_the_shared_worker_logic():
    from dataclasses import replace

    job = replace(_job(), status=WorkflowJobStatus.RUNNING)
    runner = FakeRunner(job)

    # first delivery of a RUNNING job: left alone, exactly like the Lambda worker
    assert dispatch_task.main(_environment(job, receive_count="1"), runner_factory=lambda: runner) == 0
    assert runner.operations.job.status is WorkflowJobStatus.RUNNING
    # a garbage receive count falls back to 1
    assert dispatch_task.main(_environment(job, receive_count="not-a-number"), runner_factory=lambda: runner) == 0
    assert runner.operations.job.status is WorkflowJobStatus.RUNNING


@pytest.mark.parametrize("body", [None, "", "garbage", "{}"])
def test_a_poison_message_exits_6_before_any_expensive_start_up(body):
    started: list[bool] = []

    def factory():
        started.append(True)
        raise AssertionError("must not start the runner for a malformed message")

    environment = {} if body is None else {dispatch_task.MESSAGE_ENVIRONMENT_VARIABLE: body}

    assert dispatch_task.main(environment, runner_factory=factory) == dispatch_task.EXIT_POISON_MESSAGE == 6
    assert started == []


def test_an_unknown_job_exits_6():
    job = _job()
    runner = FakeRunner(job)
    runner.operations.job = None

    assert dispatch_task.main(_environment(job), runner_factory=lambda: runner) == 6


def test_an_unexpected_error_is_a_transient_exit_7_without_leaking_the_message(capfd):
    job = _job()

    assert dispatch_task.main(_environment(job), runner_factory=lambda: FakeRunner(job, explode=True)) == dispatch_task.EXIT_TRANSIENT == 7
    captured = capfd.readouterr()
    assert "/secret/path" not in captured.out + captured.err


def test_start_up_failures_have_distinct_exit_codes():
    from ap_agent.worker.__main__ import OcrUnavailableError
    from ap_agent.worker.config import WorkerConfigurationError

    job = _job()

    def bad_configuration():
        raise WorkerConfigurationError("x")

    def no_ocr():
        raise OcrUnavailableError("x")

    assert dispatch_task.main(_environment(job), runner_factory=bad_configuration) == dispatch_task.EXIT_CONFIGURATION == 4
    assert dispatch_task.main(_environment(job), runner_factory=no_ocr) == dispatch_task.EXIT_OCR_UNAVAILABLE == 5


def test_the_hard_deadline_is_off_by_default_and_ignores_garbage():
    assert dispatch_task._start_deadline({}) is None
    assert dispatch_task._start_deadline({dispatch_task.MAX_SECONDS_ENVIRONMENT_VARIABLE: "x"}) is None
    assert dispatch_task._start_deadline({dispatch_task.MAX_SECONDS_ENVIRONMENT_VARIABLE: "0"}) is None


def test_the_hard_deadline_exits_8_when_it_expires(monkeypatch):
    exits: list[int] = []
    monkeypatch.setattr(dispatch_task.os, "_exit", exits.append)
    timer = dispatch_task._start_deadline({dispatch_task.MAX_SECONDS_ENVIRONMENT_VARIABLE: "0.01"})

    assert timer is not None
    timer.join(timeout=5)
    assert exits == [dispatch_task.EXIT_DEADLINE] == [8]


def test_a_finished_job_cancels_the_deadline(monkeypatch):
    job = _job()
    timers = []
    real = dispatch_task._start_deadline

    def spy(env):
        timer = real(env)
        timers.append(timer)
        return timer

    monkeypatch.setattr(dispatch_task, "_start_deadline", spy)
    runner = FakeRunner(job)

    assert dispatch_task.main(_environment(job, **{dispatch_task.MAX_SECONDS_ENVIRONMENT_VARIABLE: "600"}), runner_factory=lambda: runner) == 0
    assert timers[0] is not None
    timers[0].join(timeout=5)
    assert not timers[0].is_alive()


def test_the_task_entry_point_and_dispatcher_agree_on_the_variable_names():
    assert dispatch_task.MESSAGE_ENVIRONMENT_VARIABLE == fd.MESSAGE_ENVIRONMENT_VARIABLE
    assert dispatch_task.RECEIVE_COUNT_ENVIRONMENT_VARIABLE == fd.RECEIVE_COUNT_ENVIRONMENT_VARIABLE


def test_the_task_entry_point_is_importable_without_the_ocr_stack():
    code = (
        "import sys, ap_agent.worker.dispatch_task;"
        "print([m for m in ('paddle','paddleocr','pytesseract','fitz','cv2','numpy','PIL','pandas') if m in sys.modules])"
    )

    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip() == "[]"

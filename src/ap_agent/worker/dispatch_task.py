"""`python -m ap_agent.worker.dispatch_task` -- run ONE queued job, then exit (M11E.1).

Used only by the on-demand ECS Fargate *fallback* (`deploy/aws/fargate-fallback.yaml`), which exists for the case
where real PaddleOCR cannot finish inside Lambda's limits. EventBridge Pipes starts one task per SQS message and
passes the message body in `AP_AGENT_DISPATCH_MESSAGE`; this module handles it with exactly the same logic as the
Lambda worker (`ap_agent.worker.lambda_handler.process_dispatch_record`), so duplicate messages are no-ops.

Exit codes: 0 handled (processed or skipped), 4 configuration, 5 OCR unavailable, 6 unusable message,
7 transient failure. The task never loops and never polls: no always-on process.
"""

from __future__ import annotations

import logging
import os
import sys

EXIT_OK = 0
EXIT_CONFIGURATION = 4
EXIT_OCR_UNAVAILABLE = 5
EXIT_POISON_MESSAGE = 6
EXIT_TRANSIENT = 7

MESSAGE_ENVIRONMENT_VARIABLE = "AP_AGENT_DISPATCH_MESSAGE"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from ap_agent.aws.secrets import SecretLoadError, load_ssm_parameters_into_environment
    from ap_agent.config.deployment import HostedConfigurationError
    from ap_agent.worker.__main__ import OcrUnavailableError, build_runner
    from ap_agent.worker.config import WorkerConfigurationError, load_worker_config
    from ap_agent.worker.lambda_handler import MessageOutcome, PoisonMessageError, process_dispatch_record

    body = os.environ.get(MESSAGE_ENVIRONMENT_VARIABLE, "")

    from ap_agent.aws.paddle_cache import prepare_paddlex_cache

    prepare_paddlex_cache()  # no-op unless the image bakes its OCR assets read-only

    try:
        load_ssm_parameters_into_environment()
        runner = build_runner(load_worker_config(), warm_up_ocr=True)
    except (SecretLoadError, WorkerConfigurationError, HostedConfigurationError) as error:
        print(f"worker task: configuration error ({type(error).__name__}).", file=sys.stderr)
        return EXIT_CONFIGURATION
    except OcrUnavailableError as error:
        print(f"worker task: the OCR provider could not be initialised ({error}).", file=sys.stderr)
        return EXIT_OCR_UNAVAILABLE

    try:
        outcome: MessageOutcome = process_dispatch_record(
            {"body": body, "attributes": {"ApproximateReceiveCount": "1"}}, runner=runner
        )
    except PoisonMessageError as error:
        print(f"worker task: unusable dispatch message ({error}).", file=sys.stderr)
        return EXIT_POISON_MESSAGE
    except Exception as error:  # noqa: BLE001
        print(f"worker task: transient failure ({type(error).__name__}).", file=sys.stderr)
        return EXIT_TRANSIENT

    print(f"worker task: {outcome.value}.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

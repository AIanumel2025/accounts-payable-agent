"""Safe operational messages and the engine-event bridge for job timelines.

New in M11D. Job events are an operator-visible timeline, so they carry only
safe operational metadata: a controlled event type, stage, status, attempt
and a short message. Never invoice content, DSNs, identity headers,
filesystem paths or raw exception dumps.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from ap_agent.models.operations import WorkflowJob
from ap_agent.models.orchestration import OrchestrationEvent
from ap_agent.repositories.operations_repository import OperationsRepository

__all__ = ["sanitize_job_message", "safe_error_code", "build_job_event_sink"]

_LOGGER = logging.getLogger("ap_agent.worker")

_DSN_PATTERN = re.compile(r"postgres(?:ql)?://\S+", flags=re.IGNORECASE)
_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:\\|/)[^\s\"']+")
_HEADER_PATTERN = re.compile(r"X-(?:Tenant|Actor|Authenticated)[\w-]*\s*[:=]\s*\S+", flags=re.IGNORECASE)
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def sanitize_job_message(message: str, *, limit: int = 300) -> str:
    cleaned = _DSN_PATTERN.sub("[redacted]", message)
    cleaned = _HEADER_PATTERN.sub("[redacted]", cleaned)
    cleaned = _PATH_PATTERN.sub("[path]", cleaned)
    cleaned = " ".join(cleaned.split())

    return (cleaned[:limit] or "event")


def safe_error_code(candidate: str, *, default: str = "JOB_FAILED") -> str:
    normalized = re.sub(r"[^A-Z0-9_]", "_", candidate.upper())[:64]

    return normalized if _ERROR_CODE_PATTERN.fullmatch(normalized) else default


def build_job_event_sink(
    repository: OperationsRepository, job: WorkflowJob
) -> Callable[[OrchestrationEvent], None]:
    """Engine observability sink. The engine already swallows a raising
    sink, but failures are logged (type only) so a broken timeline is
    noticed without ever leaking content."""

    def sink(event: OrchestrationEvent) -> None:
        stage: Optional[str] = event.stage.value if event.stage is not None else None

        try:
            repository.append_event(
                tenant_id=job.tenant_id,
                job_id=job.job_id,
                event_type=event.event_type.value,
                status=event.status,
                stage=stage,
                attempt_number=int(event.attempt_number or 0),
                message=sanitize_job_message(event.message),
            )

            if event.event_type.value == "STAGE_STARTED" and stage is not None:
                repository.update_progress(job, stage=stage)
        except Exception as error:  # noqa: BLE001 - observability must not break execution
            _LOGGER.warning("Job event could not be recorded (%s).", type(error).__name__)

    return sink

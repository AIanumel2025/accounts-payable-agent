"""Phase 8 observability: structured events, not `print()`.

New in M9 (task §14). The notebook's Phase 8 Cell 5 reports progress with
bare `print(...)` calls interleaved with its execution loop -- fine for a
notebook cell, unusable as a library API (nothing downstream of a print
statement can consume it as data, redirect it, or turn it off). Every
stage transition the engine makes is already a typed
`ap_agent.models.orchestration.OrchestrationEvent`
(`ap_agent.orchestration.engine.execute_invoice_workflow` builds every one
of them via `append_orchestration_event`, whether or not a caller ever
looks at them); this module gives a caller a way to *receive* that stream
live (`event_sink`, task §14: "Optional event sink/callback") and a
ready-made renderer that turns it (or a finished `InvoiceWorkflowResult`)
into the notebook/CLI-friendly progress format the task brief specifies,
without the core orchestration modules themselves calling `print` (none of
`ap_agent.orchestration.{routing,engine,handlers,batch}` do).

No credentials, DSNs or invoice payloads are ever rendered here: every
formatter below only reads `OrchestrationStage`/`StageExecutionStatus`/
`WorkflowRoute` values, counts, and the already-redacted
`StageExecutionRecord.error_message`
(`ap_agent.orchestration.engine.safe_orchestration_error_message` strips
any `postgres://`/`postgresql://` connection string before it ever reaches
a `StageExecutionRecord`) -- never a phase result's own fields, which may
carry invoice content (task §14: "Do not expose credentials, DSNs or
invoice payloads in routine logs.").
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, TextIO

from ap_agent.models.orchestration import (
    InvoiceWorkflowResult,
    OrchestrationEvent,
    StageExecutionRecord,
)

__all__ = [
    "EventSink",
    "CollectingEventSink",
    "format_stage_line",
    "format_invoice_progress",
    "ConsoleProgressObserver",
]


EventSink = Callable[[OrchestrationEvent], None]


@dataclass
class CollectingEventSink:
    """The simplest possible `EventSink`: appends every event it receives
    to an in-process list, for a test or caller that wants to assert on
    the exact event sequence without parsing rendered text."""

    events: list[OrchestrationEvent] = field(default_factory=list)

    def __call__(self, event: OrchestrationEvent) -> None:
        self.events.append(event)


def format_stage_line(record: StageExecutionRecord) -> str:
    """Render one `StageExecutionRecord` as
    ``"  <STAGE>             <STATUS>"``, matching the task §14 example
    layout. A retried stage's later attempts are rendered as separate
    lines (one per `StageExecutionRecord`, one per attempt) so a retry is
    visible rather than silently collapsed."""

    label = record.stage.value

    if record.attempt_number > 1:
        label = f"{label} (attempt {record.attempt_number})"

    return f"  {label:<28}{record.status.value}"


def format_invoice_progress(
    result: InvoiceWorkflowResult,
    *,
    index: int | None = None,
    total: int | None = None,
) -> str:
    """Render one finished `InvoiceWorkflowResult` as the multi-line
    progress block from task §14:

    ```
    [1/4] invoice_0001.pdf
      INGESTION             SUCCEEDED
      ...
      TERMINAL ROUTE        HUMAN_REVIEW
    ```

    Only the document's filename is rendered, never its full path (which
    could reveal a local filesystem layout) and never any field of a
    phase result itself.
    """

    lines: list[str] = []

    header = result.source_path.name

    if index is not None and total is not None:
        header = f"[{index}/{total}] {header}"

    lines.append(header)

    for record in result.stage_records:
        lines.append(format_stage_line(record))

    lines.append(f"  {'TERMINAL ROUTE':<28}{result.terminal_route}")

    return "\n".join(lines)


class ConsoleProgressObserver:
    """A notebook/CLI-friendly `EventSink` that prints one line per
    `STAGE_COMPLETED`/`STAGE_FAILED`/`REVIEW_ROUTED`/`WORKFLOW_COMPLETED`
    event as it happens, and can render the final per-invoice summary
    block from a finished `InvoiceWorkflowResult` (task §14's example
    layout).

    This is the *only* place in the orchestration package that writes to
    a stream; it is opt-in (a caller passes an instance as
    `execute_invoice_workflow`'s/`execute_batch_workflow`'s `event_sink`)
    and never required by the engine itself.
    """

    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self._stream = stream

    def __call__(self, event: OrchestrationEvent) -> None:
        stage_text = event.stage.value if event.stage is not None else "WORKFLOW"
        print(f"{stage_text}: {event.event_type.value} ({event.status})", file=self._stream)

    def render_invoice_result(
        self,
        result: InvoiceWorkflowResult,
        *,
        index: int | None = None,
        total: int | None = None,
    ) -> None:
        print(format_invoice_progress(result, index=index, total=total), file=self._stream)

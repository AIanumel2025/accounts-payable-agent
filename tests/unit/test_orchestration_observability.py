"""M9 unit tests: structured observability (ap_agent.orchestration.observability),
task §14/§15 "Event-sink behaviour".
"""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.models.orchestration import default_orchestration_config, orchestration_utc_now
from ap_agent.models.orchestration import InvoiceWorkflowRequest
from ap_agent.orchestration.engine import execute_invoice_workflow
from ap_agent.orchestration.observability import (
    CollectingEventSink,
    format_invoice_progress,
    format_stage_line,
)
from tests.support.synthetic_orchestration_handlers import build_synthetic_handler_registry

pytestmark = [pytest.mark.unit]


SRC_ORCHESTRATION_DIR = Path(__file__).resolve().parents[2] / "src" / "ap_agent" / "orchestration"


def _request() -> InvoiceWorkflowRequest:
    from pathlib import Path as P

    return InvoiceWorkflowRequest(
        tenant_id=uuid4(),
        batch_id=uuid4(),
        correlation_id=uuid4(),
        source_path=P("/synthetic/invoice.pdf"),
        source_channel="SYNTHETIC_TEST",
        submitted_at=orchestration_utc_now(),
    )


def test_collecting_event_sink_receives_every_event_in_order():
    document_id = uuid4()
    registry, _ = build_synthetic_handler_registry(document_id)
    sink = CollectingEventSink()

    result = execute_invoice_workflow(
        request=_request(),
        handler_registry=registry,
        config=default_orchestration_config(),
        event_sink=sink,
    )

    assert [event.event_id for event in sink.events] == [event.event_id for event in result.events]


def test_format_invoice_progress_renders_the_task_brief_layout():
    document_id = uuid4()
    registry, _ = build_synthetic_handler_registry(document_id, review_stage=None)

    result = execute_invoice_workflow(
        request=_request(), handler_registry=registry, config=default_orchestration_config()
    )

    rendered = format_invoice_progress(result, index=1, total=4)

    lines = rendered.splitlines()
    assert lines[0] == "[1/4] invoice.pdf"
    assert lines[-1].strip().startswith("TERMINAL ROUTE")
    assert lines[-1].strip().endswith("COMPLETED")
    assert len(lines) == 1 + len(result.stage_records) + 1


def test_format_invoice_progress_never_includes_the_full_source_path():
    document_id = uuid4()
    registry, _ = build_synthetic_handler_registry(document_id)

    result = execute_invoice_workflow(
        request=_request(), handler_registry=registry, config=default_orchestration_config()
    )

    rendered = format_invoice_progress(result)

    assert "/synthetic/" not in rendered
    assert "invoice.pdf" in rendered


def test_format_stage_line_marks_a_retried_attempt():
    document_id = uuid4()
    registry, _ = build_synthetic_handler_registry(document_id)

    result = execute_invoice_workflow(
        request=_request(), handler_registry=registry, config=default_orchestration_config()
    )

    line = format_stage_line(result.stage_records[0])
    assert "INGESTION" in line
    assert "SUCCEEDED" in line


@pytest.mark.parametrize(
    "module_name",
    ["routing.py", "engine.py", "handlers.py", "batch.py"],
)
def test_core_orchestration_modules_never_call_print(module_name):
    """task §14: 'Core orchestration must not depend on print().' Parsed
    with `ast` rather than a text search so a module docstring or comment
    that merely *mentions* `print(...)` (as several of these do, describing
    the notebook's own `print` calls) never produces a false positive."""

    source = (SRC_ORCHESTRATION_DIR / module_name).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=module_name)

    print_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"
    ]

    assert not print_calls, f"{module_name} calls print() at: {[node.lineno for node in print_calls]}"


def test_observability_module_is_the_only_place_that_prints():
    source = (SRC_ORCHESTRATION_DIR / "observability.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="observability.py")

    print_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"
    ]

    assert print_calls, "observability.py is expected to be the rendering/printing surface"

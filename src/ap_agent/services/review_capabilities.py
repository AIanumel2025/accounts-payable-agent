"""Advisory command-capability projection for the human-review interface.

New in M11C (task §6). The M10 detail payload carries neither the workflow
revision, nor whether the case is assigned to the *calling* actor, nor the
exact correctable values/evidence ids `validate_review_command` checks a
correction against -- all of which a client needs to build a command that
can succeed. Rather than duplicating that policy in TypeScript, this
module derives it from the same authoritative sources the command
executors use (`ReviewCommandContext`, `InterfaceConfig`, the persisted
decision history) and returns it read-only.

Every value here is **advisory**: `execute_*_command` re-reads and
re-validates everything inside a locked transaction, so a stale or forged
capability can never authorise anything. This module never mutates
anything and never exposes a tenant id, actor id, credential or
configuration value.
"""

from __future__ import annotations

from typing import Any, Optional

from ap_agent.models.interface import (
    HumanReviewDisposition,
    InterfaceActor,
    InterfaceConfig,
    InvoiceDetailRecord,
    ReviewAction,
    ReviewCaseStatus,
    ReviewCommandContext,
    interface_permissions_for_role,
)
from ap_agent.models.normalization import InvoiceFieldName
from ap_agent.models.review_evidence import EVIDENCE_TYPE_SOURCE_DOCUMENT, generic_evidence_option
from ap_agent.services.review_commands import (
    ACTIONS_REQUIRING_NOTES,
    ACTIONS_REQUIRING_REASON_CODES,
    DECISION_DISPOSITIONS,
)

__all__ = [
    "SUPPORTED_TRANSACTIONAL_ACTIONS",
    "UNSUPPORTED_ACTIONS",
    "RESUME_REQUESTED_EVENT_TYPE",
    "extract_line_values",
    "build_command_capabilities",
]


# Actions with a real transactional executor behind
# `POST /api/v1/review-cases/{id}/commands` (M11C task §2).
SUPPORTED_TRANSACTIONAL_ACTIONS: tuple[ReviewAction, ...] = (
    ReviewAction.CLAIM,
    ReviewAction.RELEASE,
    ReviewAction.ACCEPT,
    ReviewAction.CORRECT,
    ReviewAction.REJECT,
    ReviewAction.RESUME_WORKFLOW,
)

# Authorised by role policy but with no executor: deferred (task §16).
UNSUPPORTED_ACTIONS: tuple[ReviewAction, ...] = (
    ReviewAction.CONFIRM_SUPPLIER,
    ReviewAction.CONFIRM_PURCHASE_ORDER,
    ReviewAction.REQUEST_INFORMATION,
    ReviewAction.ESCALATE,
)

RESUME_REQUESTED_EVENT_TYPE = "WORKFLOW_RESUME_REQUESTED"

_LINE_FIELD_KEYS = {
    InvoiceFieldName.LINE_DESCRIPTION: "description",
    InvoiceFieldName.LINE_QUANTITY: "quantity",
    InvoiceFieldName.LINE_UNIT_PRICE: "unit_price",
    InvoiceFieldName.LINE_AMOUNT: "amount",
}


def _line_item_value(raw: Any) -> Optional[str]:
    """A stored line-item value is either a bare scalar (seeded fixtures)
    or a nested normalized-field object (real Phase 4 output). Missing
    stays `None` -- never coerced to zero/empty (task §13)."""

    if isinstance(raw, dict):
        raw = raw.get("normalized_value")

    if raw is None:
        return None

    return str(raw)


def extract_line_values(
    normalized_payload: Any, line_fields: tuple[InvoiceFieldName, ...]
) -> tuple[tuple[int, InvoiceFieldName, Optional[str]], ...]:
    if not isinstance(normalized_payload, dict):
        return tuple()

    invoice_record = normalized_payload.get("invoice_record")
    line_items = invoice_record.get("line_items") if isinstance(invoice_record, dict) else None

    if not isinstance(line_items, list):
        return tuple()

    values: list[tuple[int, InvoiceFieldName, Optional[str]]] = []

    for line_item in line_items:
        if not isinstance(line_item, dict) or line_item.get("line_number") is None:
            continue

        line_number = int(line_item["line_number"])

        for field_name in line_fields:
            key = _LINE_FIELD_KEYS.get(field_name)

            if key is not None:
                values.append((line_number, field_name, _line_item_value(line_item.get(key))))

    return tuple(sorted(values, key=lambda item: (item[0], item[1].value)))


def _assignment(context: ReviewCommandContext, actor: InterfaceActor) -> str:
    if context.assigned_reviewer_id is None:
        return "UNASSIGNED"

    return "ASSIGNED_TO_ACTOR" if context.assigned_reviewer_id == actor.actor_id else "ASSIGNED_TO_OTHER"


def _resume_capability(
    *,
    context: ReviewCommandContext,
    detail: InvoiceDetailRecord,
    actor: InterfaceActor,
    permitted: set[ReviewAction],
    writes_enabled: bool,
) -> dict[str, Any]:
    decisions = detail.review_decisions
    latest = decisions[-1] if decisions else None
    already_requested = any(event.event_type == RESUME_REQUESTED_EVENT_TYPE for event in detail.timeline)

    reason: Optional[str] = None

    if not writes_enabled:
        reason = "COMMAND_MODE_VALIDATION_ONLY"
    elif ReviewAction.RESUME_WORKFLOW not in permitted:
        reason = "ACTION_NOT_PERMITTED"
    elif latest is None:
        reason = "REVIEW_DECISION_REQUIRED"
    elif context.case_status != ReviewCaseStatus.RESOLVED:
        reason = "RESOLVED_CASE_REQUIRED"
    elif latest.disposition not in {HumanReviewDisposition.APPROVED, HumanReviewDisposition.CORRECTED}:
        reason = "RESUME_DISPOSITION_INVALID"
    elif latest.reviewer_id != actor.actor_id or context.assigned_reviewer_id != actor.actor_id:
        reason = "DECISION_ACTOR_MISMATCH"
    elif already_requested:
        reason = "RESUME_ALREADY_REQUESTED"

    return {
        "eligible": reason is None,
        "already_requested": already_requested,
        "disposition": None if latest is None else latest.disposition,
        "decision_id": None if latest is None else latest.decision_id,
        "ineligible_reason": reason,
    }


def _evidence_options(context: ReviewCommandContext) -> tuple[dict[str, Any], ...]:
    """Safe structured descriptions of exactly the evidence ids a correction may cite (`available_evidence_reference_ids`).

    The source-document option (case-bound, M11E.5) comes first. An id without a stored description is still offered, with a
    generic label; an option whose id is not in the available set is never offered (options cannot widen what is accepted)."""

    described = {option.reference_id: option for option in context.evidence_options}
    available = list(context.available_evidence_reference_ids)
    ordered = [i for i in available if described.get(i) is not None and described[i].evidence_type == EVIDENCE_TYPE_SOURCE_DOCUMENT]
    ordered += [i for i in available if i not in ordered]

    return tuple(
        {
            "reference_id": reference_id,
            "evidence_type": (option := described.get(reference_id) or generic_evidence_option(reference_id)).evidence_type,
            "label": option.label,
            "page_number": option.page_number,
            "snippet": option.snippet,
        }
        for reference_id in ordered
    )


def build_command_capabilities(
    *,
    actor: InterfaceActor,
    context: ReviewCommandContext,
    detail: InvoiceDetailRecord,
    normalized_payload: Any,
    config: InterfaceConfig,
    writes_enabled: bool,
) -> dict[str, Any]:
    role_actions = set(interface_permissions_for_role(actor.role, config))
    permitted = {action for action in SUPPORTED_TRANSACTIONAL_ACTIONS if action in role_actions}
    assignment = _assignment(context, actor)
    owned_in_review = context.case_status == ReviewCaseStatus.IN_REVIEW and assignment == "ASSIGNED_TO_ACTOR"

    resume = _resume_capability(
        context=context, detail=detail, actor=actor, permitted=permitted, writes_enabled=writes_enabled
    )

    eligible: set[ReviewAction] = set()

    if ReviewAction.CLAIM in permitted and context.case_status == ReviewCaseStatus.OPEN and assignment == "UNASSIGNED":
        eligible.add(ReviewAction.CLAIM)

    if owned_in_review:
        eligible |= permitted & {
            ReviewAction.RELEASE,
            ReviewAction.ACCEPT,
            ReviewAction.CORRECT,
            ReviewAction.REJECT,
        }

    if resume["eligible"]:
        eligible.add(ReviewAction.RESUME_WORKFLOW)

    available_actions = tuple(
        {
            "action": action,
            "disposition": (
                resume["disposition"]
                if action == ReviewAction.RESUME_WORKFLOW
                else DECISION_DISPOSITIONS.get(action)
            ),
            "requires_reason_codes": action in ACTIONS_REQUIRING_REASON_CODES,
            "requires_notes": action in ACTIONS_REQUIRING_NOTES,
            "requires_corrections": action == ReviewAction.CORRECT,
        }
        for action in SUPPORTED_TRANSACTIONAL_ACTIONS
        if action in eligible
    )

    header_values = dict(context.header_field_values)
    # M11E.4: every header field the deployment explicitly allows is offered, including one the extractor never produced
    # (current_value null) -- otherwise a genuinely missing value, e.g. SUPPLIER_NAME_MISSING, could never be supplied.
    correctable_headers = tuple(
        {"field_name": field_name, "current_value": header_values.get(field_name)}
        for field_name in config.correctable_header_fields
    )
    line_values = tuple(
        {"line_number": line_number, "field_name": field_name, "current_value": value}
        for line_number, field_name, value in extract_line_values(normalized_payload, config.correctable_line_fields)
    )

    return {
        "command_mode": "COMMIT" if writes_enabled else "VALIDATION_ONLY",
        "actor_role": actor.role.value,
        "case_status": context.case_status,
        "assignment": assignment,
        "review_revision": context.review_revision,
        "workflow_revision": context.workflow_revision,
        "permitted_actions": tuple(action for action in SUPPORTED_TRANSACTIONAL_ACTIONS if action in permitted),
        "available_actions": available_actions,
        "unsupported_actions": UNSUPPORTED_ACTIONS,
        "correction_policy": {
            "header_fields": correctable_headers,
            "line_fields": config.correctable_line_fields,
            "line_numbers": context.known_invoice_line_numbers,
            "line_values": line_values,
            "evidence_reference_ids": context.available_evidence_reference_ids,
            "evidence_options": _evidence_options(context),
            "require_reason": config.require_correction_reason,
            "require_evidence": config.require_correction_evidence,
        },
        "resume": resume,
        "payment_execution": "PROHIBITED",
    }

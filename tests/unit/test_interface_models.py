"""M10 unit tests: Phase 9 interface contracts and configuration
(`ap_agent.models.interface`)."""

from __future__ import annotations

import pytest

from ap_agent.models.interface import (
    InterfaceRole,
    ReviewAction,
    default_interface_config,
    interface_permissions_for_role,
)

pytestmark = pytest.mark.unit


def test_default_config_disallows_payment_execution():
    config = default_interface_config()

    assert config.allow_payment_execution is False
    assert "EXECUTE_PAYMENT" not in {action.value for action in ReviewAction}


def test_default_config_requires_every_safety_flag():
    config = default_interface_config()

    assert config.require_authentication is True
    assert config.enforce_tenant_isolation is True
    assert config.enforce_role_permissions is True
    assert config.append_only_decisions is True
    assert config.require_idempotency_key is True
    assert config.require_observed_revision is True
    assert config.require_correction_reason is True
    assert config.require_correction_evidence is True


def test_default_config_is_a_fresh_instance_every_call():
    first = default_interface_config()
    second = default_interface_config()

    assert first == second
    assert first is not second


def test_read_only_auditor_has_no_permissions():
    config = default_interface_config()

    assert interface_permissions_for_role(InterfaceRole.READ_ONLY_AUDITOR, config) == tuple()


def test_reviewer_and_admin_can_resume_workflow():
    config = default_interface_config()

    assert ReviewAction.RESUME_WORKFLOW in interface_permissions_for_role(InterfaceRole.AP_REVIEWER, config)
    assert ReviewAction.RESUME_WORKFLOW in interface_permissions_for_role(InterfaceRole.TENANT_ADMIN, config)


def test_operator_cannot_reject_or_resume():
    config = default_interface_config()
    operator_permissions = interface_permissions_for_role(InterfaceRole.AP_OPERATOR, config)

    assert ReviewAction.REJECT not in operator_permissions
    assert ReviewAction.RESUME_WORKFLOW not in operator_permissions
    assert ReviewAction.CLAIM in operator_permissions


def test_correctable_header_and_line_fields_are_disjoint():
    config = default_interface_config()

    assert not (set(config.correctable_header_fields) & set(config.correctable_line_fields))
    assert len(config.correctable_header_fields) == len(set(config.correctable_header_fields))
    assert len(config.correctable_line_fields) == len(set(config.correctable_line_fields))


def test_every_role_is_configured_exactly_once():
    config = default_interface_config()
    configured_roles = [role for role, _ in config.role_permissions]

    assert set(configured_roles) == set(InterfaceRole)
    assert len(configured_roles) == len(set(configured_roles))

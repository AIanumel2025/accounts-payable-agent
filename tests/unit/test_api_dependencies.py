"""M10 unit tests: `ap_agent.api.dependencies` prototype header-auth
adapter (notebook cell 108's `authenticated_interface_actor`/
`parse_authenticated_at`, task §10)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ap_agent.api.config import ApiConfig
from ap_agent.api.dependencies import authenticated_interface_actor, parse_authenticated_at
from ap_agent.exceptions import ReviewAuthenticationError, TenantAccessDeniedError
from ap_agent.models.interface import InterfaceRole, interface_utc_now

pytestmark = pytest.mark.unit


def _request(api_config: ApiConfig | None = None) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(api_config=api_config or ApiConfig())))


def test_parse_authenticated_at_defaults_to_now_when_absent():
    parsed = parse_authenticated_at(None)
    assert (interface_utc_now() - parsed) < timedelta(seconds=5)


def test_parse_authenticated_at_accepts_z_suffix():
    timestamp = interface_utc_now().isoformat().replace("+00:00", "Z")
    parsed = parse_authenticated_at(timestamp)
    assert parsed.tzinfo is not None


def test_parse_authenticated_at_rejects_malformed_timestamp():
    with pytest.raises(ReviewAuthenticationError):
        parse_authenticated_at("not-a-timestamp")


def test_parse_authenticated_at_rejects_naive_timestamp():
    with pytest.raises(ReviewAuthenticationError):
        parse_authenticated_at("2026-01-01T00:00:00")


def test_parse_authenticated_at_rejects_far_future_timestamp():
    future = (interface_utc_now() + timedelta(hours=1)).isoformat()

    with pytest.raises(ReviewAuthenticationError):
        parse_authenticated_at(future)


def test_parse_authenticated_at_rejects_expired_timestamp():
    stale = (interface_utc_now() - timedelta(hours=24)).isoformat()

    with pytest.raises(ReviewAuthenticationError):
        parse_authenticated_at(stale)


def test_authenticated_actor_requires_valid_tenant_uuid():
    with pytest.raises(ReviewAuthenticationError):
        authenticated_interface_actor(
            _request(), x_tenant_id="not-a-uuid", x_actor_id="a", x_actor_role="AP_OPERATOR"
        )


def test_authenticated_actor_requires_known_role():
    with pytest.raises(ReviewAuthenticationError):
        authenticated_interface_actor(
            _request(), x_tenant_id=str(uuid4()), x_actor_id="a", x_actor_role="NOT_A_ROLE"
        )


def test_authenticated_actor_requires_actor_id():
    with pytest.raises(ReviewAuthenticationError):
        authenticated_interface_actor(
            _request(), x_tenant_id=str(uuid4()), x_actor_id="   ", x_actor_role="AP_OPERATOR"
        )


def test_authenticated_actor_succeeds_for_valid_headers():
    tenant_id = uuid4()
    actor = authenticated_interface_actor(
        _request(), x_tenant_id=str(tenant_id), x_actor_id="operator-1", x_actor_role="AP_OPERATOR"
    )

    assert actor.tenant_id == tenant_id
    assert actor.role == InterfaceRole.AP_OPERATOR
    assert actor.actor_id == "operator-1"


def test_allowed_tenant_ids_restriction_denies_other_tenants():
    allowed_tenant = uuid4()
    config = ApiConfig(allowed_tenant_ids=(allowed_tenant,))

    with pytest.raises(TenantAccessDeniedError):
        authenticated_interface_actor(
            _request(config), x_tenant_id=str(uuid4()), x_actor_id="a", x_actor_role="AP_OPERATOR"
        )

    # The allowed tenant itself must still succeed.
    actor = authenticated_interface_actor(
        _request(config), x_tenant_id=str(allowed_tenant), x_actor_id="a", x_actor_role="AP_OPERATOR"
    )
    assert actor.tenant_id == allowed_tenant


def test_unrestricted_deployment_serves_any_tenant():
    config = ApiConfig()  # allowed_tenant_ids defaults to unrestricted

    actor = authenticated_interface_actor(
        _request(config), x_tenant_id=str(uuid4()), x_actor_id="a", x_actor_role="AP_OPERATOR"
    )
    assert actor is not None

"""M11E: the FastAPI authentication boundary in Clerk mode (no database)."""

from __future__ import annotations

import uuid
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from ap_agent.api.config import ApiConfig
from ap_agent.api.dependencies import AuthenticatedActor
from ap_agent.auth.identity import ResolvedIdentity
from ap_agent.config.deployment import AuthMode, DeploymentEnvironment
from ap_agent.models.interface import InterfaceRole
from tests.support.m11e_auth import ISSUER, TestKey, clerk_config, static_verifier, tampered

pytestmark = pytest.mark.unit

TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


class FakeIdentities:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], ResolvedIdentity] = {}
        self.calls = 0

    def add(self, org: str, user: str, tenant: uuid.UUID, role: InterfaceRole, *, active=True, org_active=True):
        self.rows[(org, user)] = ResolvedIdentity(
            mapping_id=uuid.uuid4(), tenant_id=tenant, role=role, mapping_active=active, organization_active=org_active
        )

    def resolve(self, *, provider: str, external_org_id: str, external_user_id: str) -> Optional[ResolvedIdentity]:
        assert provider == "clerk"
        self.calls += 1
        return self.rows.get((external_org_id, external_user_id))

    def tenant_display_name(self, tenant_id):
        return "Example Tenant"


@pytest.fixture(scope="module")
def key() -> TestKey:
    return TestKey()


@pytest.fixture()
def identities() -> FakeIdentities:
    return FakeIdentities()


def _app(key, identities, *, allowed: tuple[uuid.UUID, ...] = ()):
    from ap_agent.api.app import create_app

    config = ApiConfig(
        environment=DeploymentEnvironment.TEST,
        auth_mode=AuthMode.CLERK_JWT,
        clerk=clerk_config(),
        allowed_tenant_ids=allowed,
    )
    app = create_app(dsn="postgresql://unused.invalid/db", api_config=config, clerk_verifier=static_verifier(key))
    app.state.identity_repository = identities

    @app.get("/_whoami")
    def whoami(actor: AuthenticatedActor) -> dict[str, str]:
        return {"tenant": str(actor.tenant_id), "role": actor.role.value, "actor": actor.actor_id}

    return TestClient(app)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_unauthenticated_request_is_rejected(key, identities):
    response = _app(key, identities).get("/_whoami")
    assert response.status_code == 401 and response.json()["errors"] == ["TOKEN_MISSING"]


@pytest.mark.parametrize("value", ["Basic abc", "Bearer", "Bearer a b", "token"])
def test_malformed_authorization_header_is_rejected(key, identities, value):
    response = _app(key, identities).get("/_whoami", headers={"Authorization": value})
    assert response.status_code == 401 and response.json()["errors"] == ["TOKEN_MALFORMED"]


def test_forged_prototype_headers_are_ignored_in_clerk_mode(key, identities):
    forged = {
        "X-Tenant-ID": str(TENANT_B),
        "X-Actor-ID": "attacker",
        "X-Actor-Role": "TENANT_ADMIN",
        "X-Authenticated-At": "2030-01-01T00:00:00+00:00",
    }
    client = _app(key, identities)

    # Headers alone authenticate nothing.
    assert client.get("/_whoami", headers=forged).status_code == 401

    # With a real token they change nothing: tenant and role come from the mapping.
    identities.add("org_a", "user_a", TENANT_A, InterfaceRole.READ_ONLY_AUDITOR)
    response = client.get("/_whoami", headers={**forged, **_bearer(key.mint(org="org_a", user="user_a"))})
    assert response.status_code == 200
    assert response.json()["tenant"] == str(TENANT_A) and response.json()["role"] == "READ_ONLY_AUDITOR"
    assert "attacker" not in response.json()["actor"]


@pytest.mark.parametrize(
    ("mint", "code"),
    [
        (lambda key: tampered(key.mint(org="org_a", user="user_a")), "TOKEN_SIGNATURE_INVALID"),
        (lambda key: key.mint(org="org_a", user="user_a", expires_in=-5), "TOKEN_EXPIRED"),
        (lambda key: key.mint(org="org_a", user="user_a", issuer="https://other.example.test"), "TOKEN_ISSUER_INVALID"),
        (lambda key: key.mint(org="org_a", user="user_a", azp="https://other.example.test"), "TOKEN_AUTHORIZED_PARTY_INVALID"),
        (lambda key: key.mint(org=None, user="user_a"), "ORGANIZATION_REQUIRED"),
    ],
)
def test_invalid_tokens_are_rejected_before_any_database_lookup(key, identities, mint, code):
    identities.add("org_a", "user_a", TENANT_A, InterfaceRole.TENANT_ADMIN)
    response = _app(key, identities).get("/_whoami", headers=_bearer(mint(key)))
    assert response.status_code == 401 and response.json()["errors"] == [code]
    assert identities.calls == 0


def test_unmapped_identity_is_forbidden(key, identities):
    response = _app(key, identities).get("/_whoami", headers=_bearer(key.mint(org="org_x", user="user_x")))
    assert response.status_code == 403 and response.json()["errors"] == ["IDENTITY_NOT_MAPPED"]


def test_inactive_membership_or_organization_is_forbidden(key, identities):
    identities.add("org_a", "disabled_user", TENANT_A, InterfaceRole.AP_OPERATOR, active=False)
    identities.add("org_b", "user_b", TENANT_B, InterfaceRole.AP_OPERATOR, org_active=False)
    client = _app(key, identities)

    for org, user in (("org_a", "disabled_user"), ("org_b", "user_b")):
        response = client.get("/_whoami", headers=_bearer(key.mint(org=org, user=user)))
        assert response.status_code == 403 and response.json()["errors"] == ["IDENTITY_MEMBERSHIP_INACTIVE"]


def test_each_organization_gets_only_its_mapped_tenant_and_role(key, identities):
    identities.add("org_a", "user_a", TENANT_A, InterfaceRole.AP_REVIEWER)
    identities.add("org_b", "user_b", TENANT_B, InterfaceRole.TENANT_ADMIN)
    # The same person in two organizations is two independent mappings.
    identities.add("org_b", "user_a", TENANT_B, InterfaceRole.READ_ONLY_AUDITOR)
    client = _app(key, identities)

    one = client.get("/_whoami", headers=_bearer(key.mint(org="org_a", user="user_a"))).json()
    two = client.get("/_whoami", headers=_bearer(key.mint(org="org_b", user="user_b"))).json()
    three = client.get("/_whoami", headers=_bearer(key.mint(org="org_b", user="user_a"))).json()

    assert (one["tenant"], one["role"]) == (str(TENANT_A), "AP_REVIEWER")
    assert (two["tenant"], two["role"]) == (str(TENANT_B), "TENANT_ADMIN")
    assert (three["tenant"], three["role"]) == (str(TENANT_B), "READ_ONLY_AUDITOR")
    assert len({one["actor"], two["actor"], three["actor"]}) == 3


def test_a_user_cannot_use_another_organizations_mapping(key, identities):
    identities.add("org_a", "user_a", TENANT_A, InterfaceRole.TENANT_ADMIN)
    response = _app(key, identities).get("/_whoami", headers=_bearer(key.mint(org="org_b", user="user_a")))
    assert response.status_code == 403 and response.json()["errors"] == ["IDENTITY_NOT_MAPPED"]


def test_allowed_tenant_list_still_applies(key, identities):
    identities.add("org_b", "user_b", TENANT_B, InterfaceRole.TENANT_ADMIN)
    response = _app(key, identities, allowed=(TENANT_A,)).get(
        "/_whoami", headers=_bearer(key.mint(org="org_b", user="user_b"))
    )
    assert response.status_code == 403 and response.json()["errors"] == ["TENANT_ACCESS_DENIED"]


def test_token_and_identifiers_never_appear_in_logs_or_responses(key, identities, caplog):
    identities.add("org_logged", "user_logged", TENANT_A, InterfaceRole.AP_OPERATOR)
    client = _app(key, identities)
    token = key.mint(org="org_logged", user="user_logged")

    with caplog.at_level("DEBUG"):
        ok = client.get("/_whoami", headers=_bearer(token))
        bad = client.get("/_whoami", headers=_bearer(tampered(token)))
        other = client.get("/_whoami", headers=_bearer(key.mint(org="org_none", user="user_none")))

    for text in (ok.text, bad.text, other.text, caplog.text):
        assert token not in text and "user_logged" not in text and "org_logged" not in text
        assert "user_none" not in text and "org_none" not in text


def test_session_endpoint_reports_role_and_tenant_name(key, identities):
    identities.add("org_a", "user_a", TENANT_A, InterfaceRole.AP_REVIEWER)
    response = _app(key, identities).get("/api/v1/session", headers=_bearer(key.mint(org="org_a", user="user_a")))
    assert response.status_code == 200
    assert response.json()["data"] == {
        "role": "AP_REVIEWER", "tenant_display_name": "Example Tenant", "auth_mode": "clerk_jwt",
    }


def test_signing_key_outage_fails_closed_with_503(identities):
    from ap_agent.api.app import create_app
    from ap_agent.auth.clerk import ClerkTokenVerifier, JwksCache

    config = clerk_config()

    def down(url, timeout):
        raise OSError("down")

    app = create_app(
        dsn="postgresql://unused.invalid/db",
        api_config=ApiConfig(environment=DeploymentEnvironment.TEST, auth_mode=AuthMode.CLERK_JWT, clerk=config),
        clerk_verifier=ClerkTokenVerifier(config, jwks=JwksCache(config, fetcher=down)),
    )
    app.state.identity_repository = identities
    token = TestKey().mint()

    response = TestClient(app).get("/api/v1/session", headers=_bearer(token))
    assert response.status_code == 503 and response.json()["errors"] == ["AUTHENTICATION_UNAVAILABLE"]


def test_prototype_mode_is_unchanged_and_ignores_bearer_tokens():
    from ap_agent.api.app import create_app

    client = TestClient(create_app(dsn="postgresql://unused.invalid/db", api_config=ApiConfig()))
    response = client.get("/api/v1/session", headers={"Authorization": "Bearer abc"})
    assert response.status_code == 401 and response.json()["errors"] == ["TENANT_ID_MISSING"]


def test_liveness_needs_no_authentication_or_dependencies():
    from ap_agent.api.app import create_app

    client = TestClient(create_app(dsn="postgresql://unused.invalid/db", api_config=ApiConfig()))
    assert client.get("/health/live").json() == {"status": "alive"}


def test_readiness_fails_without_a_database_and_leaks_nothing():
    from ap_agent.api.app import create_app

    client = TestClient(create_app(dsn="postgresql://user:hunter2@unused.invalid:1/db?connect_timeout=1", api_config=ApiConfig()))
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"configuration": "ok", "database": "failed"}}
    assert "hunter2" not in response.text and "unused.invalid" not in response.text

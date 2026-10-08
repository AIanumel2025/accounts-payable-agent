"""M11E.1: the AWS_IAM platform boundary. On the IAM-protected Function URL SigV4
occupies `Authorization`, so the Clerk token travels in a dedicated header that is
honoured ONLY in `aws_sigv4` mode. Tenant and role still come from the verified
token and the database mapping, never from headers."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from ap_agent.api.config import ApiConfig
from ap_agent.api.dependencies import CLERK_AUTHORIZATION_HEADER, AuthenticatedActor
from ap_agent.config.deployment import (
    AuthMode,
    AwsS3Config,
    DeploymentEnvironment,
    HostedConfigurationError,
    PlatformAuthMode,
    StorageMode,
    hosted_configuration_problems,
    load_platform_auth_mode,
)
from ap_agent.models.interface import InterfaceRole
from tests.support.m11e_auth import TestKey, clerk_config, static_verifier, tampered
from tests.unit.test_m11e_auth_dependency import FakeIdentities

pytestmark = pytest.mark.unit

TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
SIGV4_LOOKING = "AWS4-HMAC-SHA256 Credential=AKIATEST/20260101/eu-west-2/lambda/aws4_request, SignedHeaders=host, Signature=ab"


@pytest.fixture(scope="module")
def key() -> TestKey:
    return TestKey()


def _client(key, identities, platform: PlatformAuthMode) -> TestClient:
    from ap_agent.api.app import create_app

    hosted = platform is PlatformAuthMode.AWS_SIGV4
    config = ApiConfig(
        environment=DeploymentEnvironment.HOSTED if hosted else DeploymentEnvironment.TEST,
        auth_mode=AuthMode.CLERK_JWT,
        clerk=clerk_config(issuer="https://clerk.example.test", authorized_parties=("https://app.example.test",)),
        platform_auth_mode=platform,
        storage_mode=StorageMode.AWS_S3 if hosted else StorageMode.LOCAL,
        aws_s3=AwsS3Config(bucket="ap-agent-test-uploads", region="eu-west-2") if hosted else None,
        enable_operations=hosted,
    )
    app = create_app(
        dsn="postgresql://unused.invalid/db", api_config=config, clerk_verifier=static_verifier(key),
        artifact_store=object(), upload_staging=object(),
    )
    app.state.identity_repository = identities

    @app.get("/_whoami")
    def whoami(actor: AuthenticatedActor) -> dict[str, str]:
        return {"tenant": str(actor.tenant_id), "role": actor.role.value}

    return TestClient(app)


@pytest.fixture()
def identities() -> FakeIdentities:
    rows = FakeIdentities()
    rows.add("org_a", "user_a", TENANT_A, InterfaceRole.READ_ONLY_AUDITOR)
    return rows


def test_sigv4_mode_accepts_the_token_only_from_the_dedicated_header(key, identities):
    client = _client(key, identities, PlatformAuthMode.AWS_SIGV4)
    token = key.mint(org="org_a", user="user_a")

    response = client.get(
        "/_whoami", headers={"Authorization": SIGV4_LOOKING, CLERK_AUTHORIZATION_HEADER: f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json() == {"tenant": str(TENANT_A), "role": "READ_ONLY_AUDITOR"}


def test_sigv4_mode_never_treats_the_sigv4_authorization_header_as_a_clerk_token(key, identities):
    client = _client(key, identities, PlatformAuthMode.AWS_SIGV4)
    token = key.mint(org="org_a", user="user_a")

    assert client.get("/_whoami", headers={"Authorization": SIGV4_LOOKING}).json()["errors"] == ["TOKEN_MISSING"]
    # A valid Clerk token in the *standard* header does not authenticate in this mode either.
    assert client.get("/_whoami", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert identities.calls == 0


def test_bearer_mode_ignores_the_dedicated_header_entirely(key, identities):
    """The browser (or anything else) cannot use the alternate header against the Render/R2 mode."""

    client = _client(key, identities, PlatformAuthMode.BEARER)
    token = key.mint(org="org_a", user="user_a")

    response = client.get("/_whoami", headers={CLERK_AUTHORIZATION_HEADER: f"Bearer {token}"})
    assert response.status_code == 401 and response.json()["errors"] == ["TOKEN_MISSING"]

    # ... and it never overrides the standard header.
    response = client.get(
        "/_whoami",
        headers={"Authorization": f"Bearer {tampered(token)}", CLERK_AUTHORIZATION_HEADER: f"Bearer {token}"},
    )
    assert response.status_code == 401 and response.json()["errors"] == ["TOKEN_SIGNATURE_INVALID"]
    assert identities.calls == 0


def test_forged_tenant_role_and_actor_headers_are_ignored_in_sigv4_mode(key, identities):
    client = _client(key, identities, PlatformAuthMode.AWS_SIGV4)
    forged = {
        "X-Tenant-ID": str(TENANT_B), "X-Actor-ID": "attacker", "X-Actor-Role": "TENANT_ADMIN",
        "X-Authenticated-At": "2030-01-01T00:00:00+00:00",
    }

    assert client.get("/_whoami", headers=forged).status_code == 401

    response = client.get(
        "/_whoami", headers={**forged, CLERK_AUTHORIZATION_HEADER: f"Bearer {key.mint(org='org_a', user='user_a')}"}
    )
    assert response.json() == {"tenant": str(TENANT_A), "role": "READ_ONLY_AUDITOR"}


@pytest.mark.parametrize("value", ["Basic abc", "Bearer", "Bearer a b", "token"])
def test_malformed_dedicated_header_is_rejected(key, identities, value):
    response = _client(key, identities, PlatformAuthMode.AWS_SIGV4).get(
        "/_whoami", headers={CLERK_AUTHORIZATION_HEADER: value}
    )
    assert response.status_code == 401 and response.json()["errors"] == ["TOKEN_MALFORMED"]


def test_tampered_and_unmapped_identities_are_still_refused_in_sigv4_mode(key, identities):
    client = _client(key, identities, PlatformAuthMode.AWS_SIGV4)
    good = key.mint(org="org_a", user="user_a")

    assert client.get("/_whoami", headers={CLERK_AUTHORIZATION_HEADER: f"Bearer {tampered(good)}"}).status_code == 401

    stranger = key.mint(org="org_other", user="user_a")
    response = client.get("/_whoami", headers={CLERK_AUTHORIZATION_HEADER: f"Bearer {stranger}"})
    assert response.status_code == 403 and response.json()["errors"] == ["IDENTITY_NOT_MAPPED"]


def test_platform_mode_defaults_to_bearer_and_rejects_unknown_values():
    assert load_platform_auth_mode({}) is PlatformAuthMode.BEARER
    assert load_platform_auth_mode({"AP_AGENT_PLATFORM_AUTH_MODE": "AWS_SIGV4"}) is PlatformAuthMode.AWS_SIGV4

    with pytest.raises(HostedConfigurationError):
        load_platform_auth_mode({"AP_AGENT_PLATFORM_AUTH_MODE": "anything"})


def _problems(**overrides):
    values = dict(
        environment=DeploymentEnvironment.HOSTED, auth_mode=AuthMode.CLERK_JWT,
        clerk=clerk_config(), storage_mode=StorageMode.AWS_S3,
        s3=None, artifact_root_configured=False, operations_enabled=True, requires_storage=True,
        aws_s3=AwsS3Config(bucket="ap-agent-test-uploads", region="eu-west-2"),
        platform_auth_mode=PlatformAuthMode.AWS_SIGV4,
    )
    values.update(overrides)
    return hosted_configuration_problems(**values)


def test_valid_aws_hosted_configuration_has_no_problems():
    assert _problems() == ()


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"auth_mode": AuthMode.PROTOTYPE_HEADERS}, "AWS_SIGV4_REQUIRES_AUTH_MODE_CLERK_JWT"),
        ({"environment": DeploymentEnvironment.DEVELOPMENT}, "AWS_SIGV4_REQUIRES_HOSTED_ENVIRONMENT"),
        ({"aws_s3": None}, "AWS_S3_CONFIGURATION_INCOMPLETE"),
        ({"aws_s3": AwsS3Config(bucket="ap-agent-test-uploads", region="auto")}, "S3_REGION_MALFORMED"),
        ({"aws_s3": AwsS3Config(bucket="Bad_Bucket", region="eu-west-2")}, "S3_BUCKET_MALFORMED"),
        ({"storage_mode": StorageMode.LOCAL}, "HOSTED_REQUIRES_OBJECT_STORAGE"),
    ],
)
def test_unsafe_aws_configurations_fail_closed(overrides, code):
    assert code in _problems(**overrides)

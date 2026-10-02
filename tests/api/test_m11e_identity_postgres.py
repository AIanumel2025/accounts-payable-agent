"""M11E identity mapping and Clerk-authenticated API (real PostgreSQL).

Runs against the isolated `ap_agent_m8_test` database. The runtime role is
the freshly created least-privilege role from `tests/api/conftest.py`; the
owner DSN plays the part of the approved migration/admin DSN used by the
administrative CLI.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ap_agent.api.config import ApiConfig
from ap_agent.artifacts.s3 import S3ArtifactStore
from ap_agent.auth import admin
from ap_agent.auth.identity import IdentityRepository
from ap_agent.config.deployment import AuthMode, DeploymentEnvironment, S3StorageConfig, StorageMode
from ap_agent.db.connection import open_connection
from tests.support.fake_s3 import FakeS3Client
from tests.support.m11e_auth import TestKey, clerk_config, static_verifier

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "invoices"
FLAT_PNG = FIXTURES / "08181_flat_document.png"
SUBMISSIONS = "/api/v1/operations/submissions"


def _ids() -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:12]
    return f"org_{suffix}", f"user_{suffix}"


def _new_tenant(runtime_dsn, local_config) -> uuid.UUID:
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    tenant_id = uuid.uuid4()
    PostgresMemoryRepository(runtime_dsn, local_config).register_tenant(
        tenant_id=tenant_id, tenant_key=f"m11e-{tenant_id.hex[:10]}", display_name="M11E Tenant"
    )
    return tenant_id


@pytest.fixture(scope="module")
def key() -> TestKey:
    return TestKey()


# -- schema, privileges, administration ---------------------------------------


def test_identity_tables_are_migrated(owner_dsn, local_config, applied_migrations):
    assert "0005_identity_mappings" in {item["migration_id"] for item in applied_migrations}


def test_runtime_role_can_read_but_never_write_the_mapping(owner_dsn, runtime_dsn, local_config, runtime_role_ready):
    import psycopg

    tenant_id = _new_tenant(runtime_dsn, local_config)
    org, user = _ids()
    admin.register_mapping(owner_dsn, local_config, tenant_id=tenant_id, external_org_id=org, external_user_id=user, role="AP_OPERATOR")

    resolved = IdentityRepository(runtime_dsn, local_config).resolve(provider="clerk", external_org_id=org, external_user_id=user)
    assert resolved is not None and resolved.active and resolved.tenant_id == tenant_id

    statements = [
        "UPDATE ap_agent.identity_mappings SET interface_role = 'TENANT_ADMIN';",
        "DELETE FROM ap_agent.identity_mappings;",
        "UPDATE ap_agent.identity_organizations SET status = 'ACTIVE';",
        "INSERT INTO ap_agent.identity_organizations (provider, external_org_id, tenant_id) VALUES ('clerk', 'org_x', gen_random_uuid());",
    ]

    for statement in statements:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with open_connection(runtime_dsn, local_config) as connection:
                connection.execute(statement)


def test_registration_is_idempotent_and_refuses_conflicts(owner_dsn, runtime_dsn, local_config):
    tenant_id = _new_tenant(runtime_dsn, local_config)
    other_tenant = _new_tenant(runtime_dsn, local_config)
    org, user = _ids()

    record, action = admin.register_mapping(owner_dsn, local_config, tenant_id=tenant_id, external_org_id=org, external_user_id=user, role="AP_REVIEWER")
    assert action == "CREATED" and record.role == "AP_REVIEWER"

    again, action = admin.register_mapping(owner_dsn, local_config, tenant_id=tenant_id, external_org_id=org, external_user_id=user, role="AP_REVIEWER")
    assert action == "UNCHANGED" and again.mapping_id == record.mapping_id

    with pytest.raises(admin.IdentityAdminError) as caught:
        admin.register_mapping(owner_dsn, local_config, tenant_id=tenant_id, external_org_id=org, external_user_id=user, role="TENANT_ADMIN")
    assert caught.value.code == "MAPPING_ROLE_CONFLICT"

    with pytest.raises(admin.IdentityAdminError) as caught:
        admin.register_mapping(owner_dsn, local_config, tenant_id=other_tenant, external_org_id=org, external_user_id="user_second", role="AP_REVIEWER")
    assert caught.value.code == "ORGANIZATION_BOUND_TO_OTHER_TENANT"

    org_two, user_two = _ids()
    with pytest.raises(admin.IdentityAdminError) as caught:
        admin.register_mapping(owner_dsn, local_config, tenant_id=tenant_id, external_org_id=org_two, external_user_id=user_two, role="AP_REVIEWER")
    assert caught.value.code == "TENANT_ALREADY_HAS_ORGANIZATION"

    with pytest.raises(admin.IdentityAdminError) as caught:
        admin.register_mapping(owner_dsn, local_config, tenant_id=uuid.uuid4(), external_org_id=org_two, external_user_id=user_two, role="AP_REVIEWER")
    assert caught.value.code == "TENANT_NOT_FOUND"

    with pytest.raises(admin.IdentityAdminError) as caught:
        admin.register_mapping(owner_dsn, local_config, tenant_id=tenant_id, external_org_id=org, external_user_id="u2", role="SUPERUSER")
    assert caught.value.code == "ROLE_INVALID"

    with pytest.raises(ValueError):
        admin.register_mapping(owner_dsn, local_config, tenant_id=tenant_id, external_org_id="org id with spaces", external_user_id="u", role="AP_REVIEWER")


def test_update_disable_enable_and_inspect(owner_dsn, runtime_dsn, local_config):
    tenant_id = _new_tenant(runtime_dsn, local_config)
    org, user = _ids()
    admin.register_mapping(owner_dsn, local_config, tenant_id=tenant_id, external_org_id=org, external_user_id=user, role="AP_OPERATOR")
    repository = IdentityRepository(runtime_dsn, local_config)

    record, action = admin.update_mapping(owner_dsn, local_config, external_org_id=org, external_user_id=user, role="TENANT_ADMIN")
    assert (action, record.role) == ("UPDATED", "TENANT_ADMIN")
    assert admin.update_mapping(owner_dsn, local_config, external_org_id=org, external_user_id=user, role="TENANT_ADMIN")[1] == "UNCHANGED"

    record, _ = admin.update_mapping(owner_dsn, local_config, external_org_id=org, external_user_id=user, status="DISABLED")
    assert record.status == "DISABLED" and record.disabled_at is not None
    assert not repository.resolve(provider="clerk", external_org_id=org, external_user_id=user).active

    record, _ = admin.update_mapping(owner_dsn, local_config, external_org_id=org, external_user_id=user, status="ACTIVE")
    assert record.status == "ACTIVE" and record.disabled_at is None
    assert repository.resolve(provider="clerk", external_org_id=org, external_user_id=user).active

    assert admin.set_organization_status(owner_dsn, local_config, external_org_id=org, status="DISABLED") == "UPDATED"
    assert not repository.resolve(provider="clerk", external_org_id=org, external_user_id=user).active
    assert admin.set_organization_status(owner_dsn, local_config, external_org_id=org, status="ACTIVE") == "UPDATED"

    with pytest.raises(admin.IdentityAdminError) as caught:
        admin.update_mapping(owner_dsn, local_config, external_org_id=org, external_user_id="nobody", role="AP_OPERATOR")
    assert caught.value.code == "MAPPING_NOT_FOUND"

    [listed] = admin.inspect_mappings(owner_dsn, local_config, tenant_id=tenant_id)
    assert (listed.external_org_id, listed.external_user_id) == (org, user)
    assert repository.resolve(provider="clerk", external_org_id="org_unknown", external_user_id=user) is None


def test_identity_columns_are_immutable_and_rows_are_never_deleted(owner_dsn, runtime_dsn, local_config):
    import psycopg

    tenant_id = _new_tenant(runtime_dsn, local_config)
    org, user = _ids()
    admin.register_mapping(owner_dsn, local_config, tenant_id=tenant_id, external_org_id=org, external_user_id=user, role="AP_OPERATOR")

    for statement in (
        f"UPDATE ap_agent.identity_mappings SET external_user_id = 'someone_else' WHERE external_org_id = '{org}';",
        f"UPDATE ap_agent.identity_mappings SET tenant_id = gen_random_uuid() WHERE external_org_id = '{org}';",
        f"DELETE FROM ap_agent.identity_mappings WHERE external_org_id = '{org}';",
        f"DELETE FROM ap_agent.identity_organizations WHERE external_org_id = '{org}';",
    ):
        with pytest.raises(psycopg.Error):
            with open_connection(owner_dsn, local_config) as connection:
                connection.execute(statement)


def test_database_constraints_reject_unknown_roles_and_inconsistent_status(owner_dsn, runtime_dsn, local_config):
    import psycopg

    tenant_id = _new_tenant(runtime_dsn, local_config)
    org, _user = _ids()

    with open_connection(owner_dsn, local_config) as connection:
        connection.execute(
            "INSERT INTO ap_agent.identity_organizations (provider, external_org_id, tenant_id) VALUES ('clerk', %s, %s);",
            (org, tenant_id),
        )

    for role, status in (("ROOT", "ACTIVE"), ("AP_OPERATOR", "SUSPENDED")):
        with pytest.raises(psycopg.errors.CheckViolation):
            with open_connection(owner_dsn, local_config) as connection:
                connection.execute(
                    "INSERT INTO ap_agent.identity_mappings (provider, external_org_id, external_user_id, tenant_id, interface_role, status) "
                    "VALUES ('clerk', %s, 'user_x', %s, %s, %s);",
                    (org, tenant_id, role, status),
                )

    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # tenant must be the organization's tenant
        with open_connection(owner_dsn, local_config) as connection:
            connection.execute(
                "INSERT INTO ap_agent.identity_mappings (provider, external_org_id, external_user_id, tenant_id, interface_role) "
                "VALUES ('clerk', %s, 'user_y', gen_random_uuid(), 'AP_OPERATOR');",
                (org,),
            )


def _cli(owner_dsn: str, *arguments: str, dsn_env: bool = True) -> subprocess.CompletedProcess:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AP_AGENT_")}

    if dsn_env:
        environment["AP_AGENT_POSTGRES_MIGRATION_DSN"] = owner_dsn

    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "manage_identity_mappings.py"), *arguments],
        capture_output=True, text=True, env=environment, cwd=REPO, timeout=60,
    )


def test_cli_registers_inspects_updates_and_never_prints_secrets(owner_dsn, runtime_dsn, local_config):
    org, user = _ids()
    created = _cli(owner_dsn, "register-tenant", "--tenant-key", f"cli-{uuid.uuid4().hex[:10]}", "--display-name", "CLI Tenant")
    assert created.returncode == 0, created.stderr
    tenant_id = created.stdout.strip().split("tenant=")[1]

    first = _cli(owner_dsn, "register", "--tenant-id", tenant_id, "--org-id", org, "--user-id", user, "--role", "AP_REVIEWER")
    second = _cli(owner_dsn, "register", "--tenant-id", tenant_id, "--org-id", org, "--user-id", user, "--role", "AP_REVIEWER")
    assert first.stdout.startswith("CREATED") and second.stdout.startswith("UNCHANGED")

    conflict = _cli(owner_dsn, "register", "--tenant-id", tenant_id, "--org-id", org, "--user-id", user, "--role", "TENANT_ADMIN")
    assert conflict.returncode == 3 and "MAPPING_ROLE_CONFLICT" in conflict.stderr

    assert _cli(owner_dsn, "disable", "--org-id", org, "--user-id", user).stdout.startswith("UPDATED")
    inspected = _cli(owner_dsn, "inspect", "--tenant-id", tenant_id)
    assert "status=DISABLED" in inspected.stdout and "1 mapping(s)." in inspected.stdout
    assert _cli(owner_dsn, "enable", "--org-id", org, "--user-id", user).stdout.startswith("UPDATED")

    from psycopg.conninfo import conninfo_to_dict

    password = conninfo_to_dict(owner_dsn).get("password")
    for result in (created, first, second, conflict, inspected):
        assert owner_dsn not in result.stdout + result.stderr
        assert not password or password not in result.stdout + result.stderr


def test_cli_requires_the_migration_dsn_and_does_not_fall_back_to_the_runtime_dsn(owner_dsn):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AP_AGENT_")}
    environment["AP_AGENT_POSTGRES_DSN"] = owner_dsn  # the runtime variable must not be used

    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "manage_identity_mappings.py"), "inspect"],
        capture_output=True, text=True, env=environment, cwd=REPO, timeout=60,
    )
    assert result.returncode == 2 and "AP_AGENT_POSTGRES_MIGRATION_DSN" in result.stderr
    assert owner_dsn not in result.stderr


# -- Clerk-authenticated API --------------------------------------------------


class HostedStack:
    def __init__(self, runtime_dsn, local_config, owner_dsn, key):
        self.owner_dsn, self.local_config, self.runtime_dsn, self.key = owner_dsn, local_config, runtime_dsn, key
        self.s3 = FakeS3Client()
        from ap_agent.api.app import create_app

        config = ApiConfig(
            environment=DeploymentEnvironment.TEST,
            auth_mode=AuthMode.CLERK_JWT,
            clerk=clerk_config(),
            storage_mode=StorageMode.S3,
            s3=S3StorageConfig(endpoint_url="http://127.0.0.1:1", bucket="example-bucket", access_key_id="a", secret_access_key="b"),
            enable_operations=True,
        )
        self.client = TestClient(
            create_app(
                dsn=runtime_dsn, memory_config=local_config, api_config=config,
                clerk_verifier=static_verifier(key), artifact_store=S3ArtifactStore(self.s3, "example-bucket"),
            )
        )

    def organization(self, role_by_user: dict[str, str]) -> tuple[uuid.UUID, str, dict[str, str]]:
        tenant_id = _new_tenant(self.runtime_dsn, self.local_config)
        org, _ = _ids()
        users = {}

        for label, role in role_by_user.items():
            users[label] = f"user_{label}_{uuid.uuid4().hex[:8]}"
            admin.register_mapping(
                self.owner_dsn, self.local_config, tenant_id=tenant_id, external_org_id=org, external_user_id=users[label], role=role
            )

        return tenant_id, org, users

    def headers(self, org: str, user: str, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key.mint(org=org, user=user)}", **extra}

    def upload(self, org: str, user: str, *, key_suffix: str = "a", **extra: str):
        return self.client.post(
            SUBMISSIONS,
            headers=self.headers(org, user, **{"Idempotency-Key": f"hosted-test-{uuid.uuid4().hex[:10]}-{key_suffix}"}, **extra),
            files={"file": (FLAT_PNG.name, io.BytesIO(FLAT_PNG.read_bytes()), "image/png")},
        )


@pytest.fixture()
def stack(runtime_dsn, local_config, owner_dsn, key) -> HostedStack:
    return HostedStack(runtime_dsn, local_config, owner_dsn, key)


def test_mapped_operator_uploads_through_the_object_store_and_nothing_runs_in_the_request(stack):
    tenant_id, org, users = stack.organization({"op": "AP_OPERATOR"})

    response = stack.upload(org, users["op"])

    assert response.status_code == 202
    job = response.json()["data"]["job"]
    assert job["status"] == "QUEUED"

    [(bucket, object_key)] = stack.s3.objects
    assert object_key.startswith(f"tenants/{tenant_id.hex}/jobs/")
    assert FLAT_PNG.name not in object_key
    assert stack.s3.count("put_object") == 1 and stack.s3.count("get_object") == 0  # no worker, no OCR in the request

    text = response.text
    assert object_key not in text and "example-bucket" not in text and "object://" not in text

    detail = stack.client.get(f"/api/v1/operations/jobs/{job['job_id']}", headers=stack.headers(org, users["op"]))
    assert detail.status_code == 200


def test_roles_are_enforced_by_the_api_from_the_database_mapping(stack):
    _tenant, org, users = stack.organization(
        {"admin": "TENANT_ADMIN", "op": "AP_OPERATOR", "reviewer": "AP_REVIEWER", "auditor": "READ_ONLY_AUDITOR"}
    )

    for label in ("admin", "op"):
        assert stack.upload(org, users[label]).status_code == 202

    for label in ("reviewer", "auditor"):
        denied = stack.upload(org, users[label])
        assert denied.status_code == 403 and denied.json()["errors"] == ["ACTION_NOT_PERMITTED"]

    for label in users:  # every role may read the queue
        assert stack.client.get("/api/v1/operations/jobs", headers=stack.headers(org, users[label])).status_code == 200

    assert stack.s3.count("put_object") == 2


def test_forged_prototype_headers_cannot_grant_a_role_or_a_tenant(stack, runtime_dsn, local_config):
    _tenant, org, users = stack.organization({"auditor": "READ_ONLY_AUDITOR"})
    other_tenant = _new_tenant(runtime_dsn, local_config)

    forged = {"X-Tenant-ID": str(other_tenant), "X-Actor-ID": "attacker", "X-Actor-Role": "TENANT_ADMIN"}
    assert stack.upload(org, users["auditor"], **forged).status_code == 403
    assert stack.client.get("/api/v1/operations/jobs", headers=forged).status_code == 401


def test_organizations_are_isolated_from_each_other(stack):
    _a, org_a, users_a = stack.organization({"op": "AP_OPERATOR"})
    _b, org_b, users_b = stack.organization({"op": "AP_OPERATOR"})

    job_a = stack.upload(org_a, users_a["op"]).json()["data"]["job"]["job_id"]

    listed_b = stack.client.get("/api/v1/operations/jobs", headers=stack.headers(org_b, users_b["op"])).json()["data"]
    assert listed_b["items"] == []
    assert stack.client.get(f"/api/v1/operations/jobs/{job_a}", headers=stack.headers(org_b, users_b["op"])).status_code == 404

    # A user of organization A presenting organization B's identifier has no mapping there.
    crossed = stack.client.get("/api/v1/operations/jobs", headers=stack.headers(org_b, users_a["op"]))
    assert crossed.status_code == 403 and crossed.json()["errors"] == ["IDENTITY_NOT_MAPPED"]


def test_disabling_a_membership_takes_effect_on_the_next_request(stack, owner_dsn, local_config):
    _tenant, org, users = stack.organization({"op": "AP_OPERATOR"})
    assert stack.client.get("/api/v1/operations/jobs", headers=stack.headers(org, users["op"])).status_code == 200

    admin.update_mapping(owner_dsn, local_config, external_org_id=org, external_user_id=users["op"], status="DISABLED")
    response = stack.client.get("/api/v1/operations/jobs", headers=stack.headers(org, users["op"]))
    assert response.status_code == 403 and response.json()["errors"] == ["IDENTITY_MEMBERSHIP_INACTIVE"]

    admin.update_mapping(owner_dsn, local_config, external_org_id=org, external_user_id=users["op"], status="ACTIVE")
    assert stack.client.get("/api/v1/operations/jobs", headers=stack.headers(org, users["op"])).status_code == 200


def test_unmapped_account_is_forbidden_and_session_reports_the_mapped_role(stack):
    _tenant, org, users = stack.organization({"reviewer": "AP_REVIEWER"})

    unmapped = stack.client.get("/api/v1/session", headers=stack.headers(org, "user_never_registered"))
    assert unmapped.status_code == 403 and unmapped.json()["errors"] == ["IDENTITY_NOT_MAPPED"]

    session = stack.client.get("/api/v1/session", headers=stack.headers(org, users["reviewer"])).json()["data"]
    assert session == {"role": "AP_REVIEWER", "tenant_display_name": "M11E Tenant", "auth_mode": "clerk_jwt"}


def test_readiness_succeeds_against_a_live_database(stack):
    response = stack.client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"configuration": "ok", "database": "ok"}}


def test_a_storage_outage_fails_the_upload_closed_and_leaves_no_job(stack):
    _tenant, org, users = stack.organization({"op": "AP_OPERATOR"})
    stack.s3.fail_on = "put_object"

    response = stack.upload(org, users["op"])
    assert response.status_code == 503 and response.json()["errors"] == ["STORAGE_UNAVAILABLE"]

    stack.s3.fail_on = None
    assert stack.client.get("/api/v1/operations/jobs", headers=stack.headers(org, users["op"])).json()["data"]["items"] == []

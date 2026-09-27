"""Shared PostgreSQL fixtures for `tests/api`'s `requires_postgres` suite.

Same DSN-sourcing safety gate as
`tests/integration/test_memory_postgres_integration.py` (see that
module's docstring): reads only `AP_AGENT_TEST_POSTGRES_DSN`, verifies its
`dbname` is `ap_agent_m8_test` before opening any connection, and drives
every RLS-sensitive test through a dedicated, freshly created
least-privilege runtime role rather than the DSN's own (migration-owner)
role.
"""

from __future__ import annotations

import os
import secrets
import uuid

import pytest

from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy

TEST_DSN_ENV_VAR = "AP_AGENT_TEST_POSTGRES_DSN"
EXPECTED_TEST_DATABASE = "ap_agent_m8_test"
RUNTIME_ROLE_NAME = "ap_agent_m10_test_runtime"


def _owner_dsn_or_skip() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR, "").strip()

    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not configured.")

    from psycopg.conninfo import conninfo_to_dict

    dbname = conninfo_to_dict(dsn).get("dbname")

    if dbname != EXPECTED_TEST_DATABASE:
        pytest.fail(
            f"{TEST_DSN_ENV_VAR} targets database {dbname!r}, expected {EXPECTED_TEST_DATABASE!r}. "
            "Refusing to run (fail-closed)."
        )

    return dsn


@pytest.fixture(scope="session")
def owner_dsn() -> str:
    return _owner_dsn_or_skip()


@pytest.fixture(scope="session")
def local_config() -> MemoryConfig:
    return MemoryConfig(transport_policy=PostgresTransportPolicy(require_ssl=True, require_channel_binding=True))


@pytest.fixture(scope="session")
def applied_migrations(owner_dsn, local_config):
    from ap_agent.db.migration_runner import apply_all_migrations

    return apply_all_migrations(owner_dsn, local_config)


@pytest.fixture(scope="session")
def runtime_role_ready(owner_dsn, local_config, applied_migrations) -> dict[str, str]:
    from ap_agent.db import roles as db_roles

    password = secrets.token_urlsafe(32)
    db_roles.create_least_privilege_role(owner_dsn, local_config, role_name=RUNTIME_ROLE_NAME, password=password)
    db_roles.grant_schema_access(owner_dsn, local_config, role_name=RUNTIME_ROLE_NAME)

    flags = db_roles.role_privilege_flags(owner_dsn, local_config, role_name=RUNTIME_ROLE_NAME)
    assert flags["superuser"] is False
    assert flags["bypassrls"] is False

    return {"role_name": RUNTIME_ROLE_NAME, "password": password}


@pytest.fixture(scope="session")
def runtime_dsn(owner_dsn, runtime_role_ready) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(owner_dsn)
    params["user"] = runtime_role_ready["role_name"]
    params["password"] = runtime_role_ready["password"]

    return make_conninfo(**params)


@pytest.fixture()
def tenant_id(runtime_dsn, local_config) -> uuid.UUID:
    from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository

    new_tenant_id = uuid.uuid4()
    PostgresMemoryRepository(runtime_dsn, local_config).register_tenant(
        tenant_id=new_tenant_id, tenant_key=f"m10-api-{new_tenant_id.hex[:8]}", display_name="M10 API Test Tenant"
    )
    return new_tenant_id


@pytest.fixture()
def seeded_case(runtime_dsn, local_config, tenant_id):
    from tests.support.review_fixtures import seed_review_case

    return seed_review_case(runtime_dsn, local_config, tenant_id=tenant_id)


@pytest.fixture()
def seeded_claimed_case(runtime_dsn, local_config, tenant_id):
    """A review case already `CLAIMED` by `ap-reviewer-1`, for tests that
    need to submit a decision/correction without first driving a real
    CLAIM command through the API."""

    from tests.support.review_fixtures import seed_review_case

    return seed_review_case(runtime_dsn, local_config, tenant_id=tenant_id, claimed_by="ap-reviewer-1")

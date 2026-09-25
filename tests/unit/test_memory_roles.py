"""M8D unit tests: `ap_agent.db.roles` identifier validation.

Pure-function tests only (no database) — this module's SQL-touching
functions (`create_least_privilege_role`, `grant_schema_access`,
`role_exists`, `role_privilege_flags`) are exercised for real against
PostgreSQL in `tests/integration/test_memory_postgres_integration.py`.
"""

from __future__ import annotations

import pytest

from ap_agent.db.roles import validate_identifier

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "name",
    ["ap_agent_app", "_leading_underscore", "Role1", "a", "role_with_numbers123"],
)
def test_validate_identifier_accepts_safe_names(name):
    assert validate_identifier(name, label="role name") == name


@pytest.mark.parametrize(
    "name",
    [
        "role; DROP TABLE ap_agent.tenants;--",
        "role name",
        "role-name",
        "1role",
        "",
        "role'name",
        "role\"name",
        "role.name",
    ],
)
def test_validate_identifier_rejects_unsafe_names(name):
    with pytest.raises(ValueError):
        validate_identifier(name, label="role name")

"""Administrative operations on the identity mapping (M11E).

Used only by `scripts/manage_identity_mappings.py`, which supplies the
migration/admin DSN. There is no public self-service path: the runtime role
cannot write these tables. Every operation is idempotent where that is
meaningful and reports a stable action code; nothing here returns or prints a
DSN, password or token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from ap_agent.auth.identity import PROVIDER_CLERK, validate_external_id
from ap_agent.config.postgres import MemoryConfig
from ap_agent.db.connection import open_connection, set_tenant_context
from ap_agent.models.interface import InterfaceRole

__all__ = [
    "IdentityAdminError",
    "MappingRecord",
    "register_tenant",
    "register_mapping",
    "update_mapping",
    "set_organization_status",
    "inspect_mappings",
]

STATUSES = ("ACTIVE", "DISABLED")


class IdentityAdminError(Exception):
    """A refused administrative change. `code` is stable and value-free."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MappingRecord:
    mapping_id: UUID
    provider: str
    external_org_id: str
    external_user_id: str
    tenant_id: UUID
    role: str
    status: str
    organization_status: str
    created_at: datetime
    updated_at: datetime
    disabled_at: Optional[datetime]


_SELECT = """
    SELECT m.mapping_id, m.provider, m.external_org_id, m.external_user_id, m.tenant_id,
           m.interface_role, m.status, o.status, m.created_at, m.updated_at, m.disabled_at
    FROM {schema}.identity_mappings AS m
    JOIN {schema}.identity_organizations AS o
      ON o.provider = m.provider AND o.external_org_id = m.external_org_id
"""


def _record(row: tuple[Any, ...]) -> MappingRecord:
    return MappingRecord(*row)


def _role(value: str) -> str:
    try:
        return InterfaceRole(value).value
    except ValueError as error:
        raise IdentityAdminError("ROLE_INVALID") from error


def _status(value: str) -> str:
    if value not in STATUSES:
        raise IdentityAdminError("STATUS_INVALID")

    return value


def _tenant_exists(cursor: Any, schema: str, tenant_id: UUID) -> bool:
    set_tenant_context(cursor, str(tenant_id))  # the tenants table is row-level secured
    cursor.execute(f"SELECT 1 FROM {schema}.tenants WHERE tenant_id = %s;", (tenant_id,))
    return cursor.fetchone() is not None


def register_tenant(
    dsn: str, config: MemoryConfig, *, tenant_id: UUID, tenant_key: str, display_name: str
) -> str:
    """Create the tenant row when absent. Returns `CREATED` or `UNCHANGED`."""

    schema = config.schema_name

    with open_connection(dsn, config) as connection:
        with connection.cursor() as cursor:
            if _tenant_exists(cursor, schema, tenant_id):
                return "UNCHANGED"

            cursor.execute(
                f"INSERT INTO {schema}.tenants (tenant_id, tenant_key, display_name) VALUES (%s, %s, %s);",
                (tenant_id, tenant_key, display_name),
            )

    return "CREATED"


def register_mapping(
    dsn: str,
    config: MemoryConfig,
    *,
    tenant_id: UUID,
    external_org_id: str,
    external_user_id: str,
    role: str,
    provider: str = PROVIDER_CLERK,
) -> tuple[MappingRecord, str]:
    """Idempotently register an organization and one member. Returns the
    record and `CREATED`/`UNCHANGED`. Refuses to change an existing mapping
    (`MAPPING_ROLE_CONFLICT` -- use `update_mapping`) or to bind an
    organization to a second tenant (`ORGANIZATION_BOUND_TO_OTHER_TENANT`)."""

    validate_external_id(external_org_id, label="organization id")
    validate_external_id(external_user_id, label="user id")
    role = _role(role)
    schema = config.schema_name
    action = "UNCHANGED"

    with open_connection(dsn, config) as connection:
        with connection.cursor() as cursor:
            if not _tenant_exists(cursor, schema, tenant_id):
                raise IdentityAdminError("TENANT_NOT_FOUND")

            cursor.execute(
                f"SELECT tenant_id FROM {schema}.identity_organizations WHERE provider = %s AND external_org_id = %s;",
                (provider, external_org_id),
            )
            organization = cursor.fetchone()

            if organization is None:
                cursor.execute(
                    f"SELECT external_org_id FROM {schema}.identity_organizations WHERE provider = %s AND tenant_id = %s;",
                    (provider, tenant_id),
                )

                if cursor.fetchone() is not None:
                    raise IdentityAdminError("TENANT_ALREADY_HAS_ORGANIZATION")

                cursor.execute(
                    f"INSERT INTO {schema}.identity_organizations (provider, external_org_id, tenant_id) VALUES (%s, %s, %s);",
                    (provider, external_org_id, tenant_id),
                )
                action = "CREATED"
            elif organization[0] != tenant_id:
                raise IdentityAdminError("ORGANIZATION_BOUND_TO_OTHER_TENANT")

            cursor.execute(
                f"SELECT interface_role, status FROM {schema}.identity_mappings "
                "WHERE provider = %s AND external_org_id = %s AND external_user_id = %s;",
                (provider, external_org_id, external_user_id),
            )
            existing = cursor.fetchone()

            if existing is None:
                cursor.execute(
                    f"INSERT INTO {schema}.identity_mappings "
                    "(provider, external_org_id, external_user_id, tenant_id, interface_role) "
                    "VALUES (%s, %s, %s, %s, %s);",
                    (provider, external_org_id, external_user_id, tenant_id, role),
                )
                action = "CREATED"
            elif existing[0] != role or existing[1] != "ACTIVE":
                raise IdentityAdminError("MAPPING_ROLE_CONFLICT")

            cursor.execute(
                _SELECT.format(schema=schema)
                + " WHERE m.provider = %s AND m.external_org_id = %s AND m.external_user_id = %s;",
                (provider, external_org_id, external_user_id),
            )
            record = _record(cursor.fetchone())

    return record, action


def update_mapping(
    dsn: str,
    config: MemoryConfig,
    *,
    external_org_id: str,
    external_user_id: str,
    role: Optional[str] = None,
    status: Optional[str] = None,
    provider: str = PROVIDER_CLERK,
) -> tuple[MappingRecord, str]:
    """Change a member's role and/or status (`ACTIVE`/`DISABLED`). Returns
    the record and `UPDATED`/`UNCHANGED`."""

    validate_external_id(external_org_id, label="organization id")
    validate_external_id(external_user_id, label="user id")

    if role is None and status is None:
        raise IdentityAdminError("NOTHING_TO_UPDATE")

    new_role = _role(role) if role is not None else None
    new_status = _status(status) if status is not None else None
    schema = config.schema_name

    with open_connection(dsn, config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT interface_role, status FROM {schema}.identity_mappings "
                "WHERE provider = %s AND external_org_id = %s AND external_user_id = %s FOR UPDATE;",
                (provider, external_org_id, external_user_id),
            )
            existing = cursor.fetchone()

            if existing is None:
                raise IdentityAdminError("MAPPING_NOT_FOUND")

            target_role = new_role or existing[0]
            target_status = new_status or existing[1]
            action = "UNCHANGED"

            if (target_role, target_status) != (existing[0], existing[1]):
                cursor.execute(
                    f"UPDATE {schema}.identity_mappings SET interface_role = %s, status = %s, "
                    "disabled_at = CASE WHEN %s = 'DISABLED' THEN COALESCE(disabled_at, transaction_timestamp()) ELSE NULL END, "
                    "updated_at = transaction_timestamp() "
                    "WHERE provider = %s AND external_org_id = %s AND external_user_id = %s;",
                    (target_role, target_status, target_status, provider, external_org_id, external_user_id),
                )
                action = "UPDATED"

            cursor.execute(
                _SELECT.format(schema=schema)
                + " WHERE m.provider = %s AND m.external_org_id = %s AND m.external_user_id = %s;",
                (provider, external_org_id, external_user_id),
            )
            record = _record(cursor.fetchone())

    return record, action


def set_organization_status(
    dsn: str, config: MemoryConfig, *, external_org_id: str, status: str, provider: str = PROVIDER_CLERK
) -> str:
    """Enable or disable a whole organization. Returns `UPDATED`/`UNCHANGED`."""

    validate_external_id(external_org_id, label="organization id")
    status = _status(status)
    schema = config.schema_name

    with open_connection(dsn, config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT status FROM {schema}.identity_organizations WHERE provider = %s AND external_org_id = %s FOR UPDATE;",
                (provider, external_org_id),
            )
            existing = cursor.fetchone()

            if existing is None:
                raise IdentityAdminError("ORGANIZATION_NOT_FOUND")

            if existing[0] == status:
                return "UNCHANGED"

            cursor.execute(
                f"UPDATE {schema}.identity_organizations SET status = %s, updated_at = transaction_timestamp() "
                "WHERE provider = %s AND external_org_id = %s;",
                (status, provider, external_org_id),
            )

    return "UPDATED"


def inspect_mappings(
    dsn: str,
    config: MemoryConfig,
    *,
    tenant_id: Optional[UUID] = None,
    external_org_id: Optional[str] = None,
    external_user_id: Optional[str] = None,
    provider: str = PROVIDER_CLERK,
) -> tuple[MappingRecord, ...]:
    schema = config.schema_name
    clauses = ["m.provider = %s"]
    parameters: list[Any] = [provider]

    if tenant_id is not None:
        clauses.append("m.tenant_id = %s")
        parameters.append(tenant_id)

    if external_org_id is not None:
        clauses.append("m.external_org_id = %s")
        parameters.append(validate_external_id(external_org_id, label="organization id"))

    if external_user_id is not None:
        clauses.append("m.external_user_id = %s")
        parameters.append(validate_external_id(external_user_id, label="user id"))

    with open_connection(dsn, config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                _SELECT.format(schema=schema) + " WHERE " + " AND ".join(clauses)
                + " ORDER BY m.external_org_id, m.external_user_id;",
                parameters,
            )
            return tuple(_record(row) for row in cursor.fetchall())

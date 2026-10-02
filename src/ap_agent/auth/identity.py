"""Resolution of an authenticated external identity to an internal tenant
and `InterfaceRole` (M11E).

The identity provider proves who the caller is and which organization is
active. The tenant and role come only from the database mapping
(`ap_agent.identity_organizations` / `ap_agent.identity_mappings`, migration
0005) -- never from a header, request body, URL parameter or token metadata.
The runtime role reads these tables without a tenant context (the lookup
precedes it) and has no write privilege on them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

from ap_agent.config.postgres import MemoryConfig
from ap_agent.db.connection import open_connection, set_tenant_context
from ap_agent.models.interface import InterfaceRole

__all__ = [
    "PROVIDER_CLERK",
    "EXTERNAL_ID_PATTERN",
    "ResolvedIdentity",
    "IdentityRepository",
    "validate_external_id",
]

PROVIDER_CLERK = "clerk"

EXTERNAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")


def validate_external_id(value: str, *, label: str) -> str:
    if not isinstance(value, str) or EXTERNAL_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is not a valid external identifier.")

    return value


@dataclass(frozen=True)
class ResolvedIdentity:
    mapping_id: UUID = field(repr=False)
    tenant_id: UUID = field(repr=False)
    role: InterfaceRole
    mapping_active: bool
    organization_active: bool

    @property
    def active(self) -> bool:
        return self.mapping_active and self.organization_active


class IdentityRepository:
    def __init__(self, dsn: str, memory_config: MemoryConfig) -> None:
        self._dsn = dsn
        self._config = memory_config

    def resolve(
        self, *, provider: str, external_org_id: str, external_user_id: str
    ) -> Optional[ResolvedIdentity]:
        schema = self._config.schema_name

        with open_connection(self._dsn, self._config) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT m.mapping_id, m.tenant_id, m.interface_role, m.status, o.status
                    FROM {schema}.identity_mappings AS m
                    JOIN {schema}.identity_organizations AS o
                      ON o.provider = m.provider
                     AND o.external_org_id = m.external_org_id
                     AND o.tenant_id = m.tenant_id
                    WHERE m.provider = %s AND m.external_org_id = %s AND m.external_user_id = %s;
                    """,
                    (provider, external_org_id, external_user_id),
                )
                row = cursor.fetchone()

        if row is None:
            return None

        mapping_id, tenant_id, role, mapping_status, organization_status = row

        return ResolvedIdentity(
            mapping_id=mapping_id,
            tenant_id=tenant_id,
            role=InterfaceRole(role),
            mapping_active=mapping_status == "ACTIVE",
            organization_active=organization_status == "ACTIVE",
        )

    def tenant_display_name(self, tenant_id: UUID) -> Optional[str]:
        with open_connection(self._dsn, self._config) as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(tenant_id))
                cursor.execute(
                    f"SELECT display_name FROM {self._config.schema_name}.tenants WHERE tenant_id = %s;",
                    (tenant_id,),
                )
                row = cursor.fetchone()

        return row[0] if row is not None else None

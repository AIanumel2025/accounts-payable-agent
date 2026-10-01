#!/usr/bin/env python3
"""Administer the identity mapping that decides a signed-in user's tenant and
role (M11E). There is no public self-service path: this CLI, run by an
operator with the approved migration/admin DSN, is the only writer.

The DSN is read only from `AP_AGENT_POSTGRES_MIGRATION_DSN` (no fallback to
the runtime DSN). It is never printed, logged or accepted on the command line.
Output contains tenant ids, Clerk ids, roles and statuses -- never secrets.

Subcommands (all idempotent where meaningful)
---------------------------------------------
register-tenant --tenant-key KEY --display-name NAME [--tenant-id UUID]
    Create the tenant row if it does not exist (a fresh UUID is generated and
    printed when --tenant-id is omitted).
register --tenant-id UUID --org-id ORG --user-id USER --role ROLE
    Bind the Clerk organization to the tenant (once) and add the member.
    Re-running the same command changes nothing; a different role is refused
    (use `update`).
update --org-id ORG --user-id USER [--role ROLE] [--status ACTIVE|DISABLED]
disable --org-id ORG --user-id USER      (same as update --status DISABLED)
enable --org-id ORG --user-id USER
disable-org --org-id ORG / enable-org --org-id ORG
inspect [--tenant-id UUID] [--org-id ORG] [--user-id USER] [--json]

Roles: TENANT_ADMIN, AP_OPERATOR, AP_REVIEWER, READ_ONLY_AUDITOR.
Exit codes: 0 ok, 2 configuration/usage, 3 refused change.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap_agent.auth import admin  # noqa: E402
from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy  # noqa: E402
from ap_agent.db.connection import load_dsn  # noqa: E402
from ap_agent.exceptions import PostgresConfigurationError  # noqa: E402
from ap_agent.models.interface import InterfaceRole  # noqa: E402

ROLES = [role.value for role in InterfaceRole]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--no-require-ssl",
        action="store_true",
        help="Allow a non-SSL connection (disposable local PostgreSQL only).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    tenant = commands.add_parser("register-tenant")
    tenant.add_argument("--tenant-id", type=uuid.UUID)
    tenant.add_argument("--tenant-key", required=True)
    tenant.add_argument("--display-name", required=True)

    register = commands.add_parser("register")
    register.add_argument("--tenant-id", type=uuid.UUID, required=True)
    register.add_argument("--org-id", required=True)
    register.add_argument("--user-id", required=True)
    register.add_argument("--role", required=True, choices=ROLES)

    update = commands.add_parser("update")
    update.add_argument("--org-id", required=True)
    update.add_argument("--user-id", required=True)
    update.add_argument("--role", choices=ROLES)
    update.add_argument("--status", choices=list(admin.STATUSES))

    for name in ("disable", "enable"):
        member = commands.add_parser(name)
        member.add_argument("--org-id", required=True)
        member.add_argument("--user-id", required=True)

    for name in ("disable-org", "enable-org"):
        organization = commands.add_parser(name)
        organization.add_argument("--org-id", required=True)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--tenant-id", type=uuid.UUID)
    inspect.add_argument("--org-id")
    inspect.add_argument("--user-id")
    inspect.add_argument("--json", action="store_true", dest="as_json")

    return parser


def _print_record(record: admin.MappingRecord, action: str | None = None) -> None:
    prefix = f"{action}: " if action else ""
    print(
        f"{prefix}tenant={record.tenant_id} org={record.external_org_id} user={record.external_user_id} "
        f"role={record.role} status={record.status} org_status={record.organization_status}"
    )


def _run(arguments: argparse.Namespace, dsn: str, config: MemoryConfig) -> int:
    command = arguments.command

    if command == "register-tenant":
        tenant_id = arguments.tenant_id or uuid.uuid4()
        action = admin.register_tenant(
            dsn, config, tenant_id=tenant_id, tenant_key=arguments.tenant_key, display_name=arguments.display_name
        )
        print(f"{action}: tenant={tenant_id}")
    elif command == "register":
        record, action = admin.register_mapping(
            dsn,
            config,
            tenant_id=arguments.tenant_id,
            external_org_id=arguments.org_id,
            external_user_id=arguments.user_id,
            role=arguments.role,
        )
        _print_record(record, action)
    elif command in {"update", "disable", "enable"}:
        status = {"disable": "DISABLED", "enable": "ACTIVE"}.get(command, getattr(arguments, "status", None))
        record, action = admin.update_mapping(
            dsn,
            config,
            external_org_id=arguments.org_id,
            external_user_id=arguments.user_id,
            role=getattr(arguments, "role", None),
            status=status,
        )
        _print_record(record, action)
    elif command in {"disable-org", "enable-org"}:
        action = admin.set_organization_status(
            dsn,
            config,
            external_org_id=arguments.org_id,
            status="DISABLED" if command == "disable-org" else "ACTIVE",
        )
        print(f"{action}: org={arguments.org_id}")
    else:  # inspect
        records = admin.inspect_mappings(
            dsn, config, tenant_id=arguments.tenant_id, external_org_id=arguments.org_id, external_user_id=arguments.user_id
        )

        if arguments.as_json:
            print(
                json.dumps(
                    [
                        {
                            "mapping_id": str(record.mapping_id),
                            "tenant_id": str(record.tenant_id),
                            "org_id": record.external_org_id,
                            "user_id": record.external_user_id,
                            "role": record.role,
                            "status": record.status,
                            "organization_status": record.organization_status,
                            "created_at": record.created_at.isoformat(),
                            "updated_at": record.updated_at.isoformat(),
                            "disabled_at": record.disabled_at.isoformat() if record.disabled_at else None,
                        }
                        for record in records
                    ],
                    indent=2,
                )
            )
        else:
            for record in records:
                _print_record(record)

            print(f"{len(records)} mapping(s).")

    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    config = MemoryConfig(
        allow_migration_dsn_fallback=False,
        transport_policy=PostgresTransportPolicy(require_ssl=not arguments.no_require_ssl),
    )

    try:
        dsn = load_dsn(config, for_migration=True)
    except PostgresConfigurationError:
        print("error: AP_AGENT_POSTGRES_MIGRATION_DSN must be set to the approved migration/admin DSN.", file=sys.stderr)
        return 2

    try:
        return _run(arguments, dsn, config)
    except admin.IdentityAdminError as error:
        print(f"refused: {error.code}", file=sys.stderr)
        return 3
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - never echo driver messages (they can contain connection details)
        print(f"error: the database operation failed ({type(error).__name__}).", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

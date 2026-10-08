"""Deliberate, non-public deployment tasks (M11E.1), run as a Lambda that has
no Function URL and is invoked only by an operator (`aws lambda invoke`).

    {"action": "migrate"}                         apply migrations, then grant the runtime role
    {"action": "identity", "args": ["register", ...]}   the existing administrative CLI

This function alone receives the migration/owner DSN. Migrations fail closed:
an integrity error or a missing DSN returns an error and changes nothing more.
The response carries migration ids, checksum prefixes and statuses -- never a
DSN, a secret or an identity value beyond what the CLI itself prints.
"""

from __future__ import annotations

import contextlib
import io
import os
from typing import Any

__all__ = ["handler", "run_migrations", "run_identity_command"]

_IDENTITY_COMMANDS = frozenset(
    {"register-tenant", "register", "update", "disable", "enable", "disable-org", "enable-org", "inspect"}
)


def run_migrations() -> dict[str, Any]:
    from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy
    from ap_agent.db.connection import load_dsn
    from ap_agent.db.migration_runner import apply_all_migrations
    from ap_agent.db.roles import grant_schema_access
    from ap_agent.exceptions import MigrationIntegrityError, PostgresConfigurationError

    config = MemoryConfig(transport_policy=PostgresTransportPolicy(require_ssl=True))

    try:
        dsn = load_dsn(config, for_migration=True)
    except PostgresConfigurationError as error:
        return {"ok": False, "error": f"MIGRATION_DSN_ERROR: {error}"}

    try:
        results = apply_all_migrations(dsn, config)
    except MigrationIntegrityError as error:
        return {"ok": False, "error": f"MIGRATION_INTEGRITY_ERROR: {error}"}

    role_name = os.environ.get("AP_AGENT_POSTGRES_RUNTIME_ROLE", "").strip()

    if role_name:
        grant_schema_access(dsn, config, role_name=role_name)

    return {
        "ok": True,
        "migrations": [
            {"id": item["migration_id"], "status": item["status"], "checksum": item["checksum"][:12]}
            for item in results
        ],
        "runtime_role_granted": bool(role_name),
    }


def run_identity_command(arguments: list[str]) -> dict[str, Any]:
    import importlib.util
    from pathlib import Path

    if not arguments or arguments[0] not in _IDENTITY_COMMANDS:
        return {"ok": False, "error": "IDENTITY_COMMAND_NOT_ALLOWED"}

    script = Path(__file__).resolve().parents[3] / "scripts" / "manage_identity_mappings.py"
    candidates = [script, Path("/var/task/scripts/manage_identity_mappings.py"), Path("/app/scripts/manage_identity_mappings.py")]
    path = next((candidate for candidate in candidates if candidate.exists()), None)

    if path is None:
        return {"ok": False, "error": "IDENTITY_SCRIPT_MISSING"}

    spec = importlib.util.spec_from_file_location("manage_identity_mappings", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    output = io.StringIO()

    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            code = module.main(list(arguments))
        except SystemExit as exit_request:
            code = int(exit_request.code or 0) if isinstance(exit_request.code, int) else 2

    return {"ok": code == 0, "exit_code": code, "output": output.getvalue()[-4000:]}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    from ap_agent.aws.secrets import load_ssm_parameters_into_environment

    load_ssm_parameters_into_environment()

    action = (event or {}).get("action")

    if action == "migrate":
        return run_migrations()

    if action == "identity":
        arguments = event.get("args")

        if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
            return {"ok": False, "error": "IDENTITY_ARGUMENTS_INVALID"}

        return run_identity_command(arguments)

    return {"ok": False, "error": "ACTION_NOT_SUPPORTED"}

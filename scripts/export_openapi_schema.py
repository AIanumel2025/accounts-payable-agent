#!/usr/bin/env python3
"""Export the M10 FastAPI application's OpenAPI schema to a file.

Used by the frontend's `npm run api:generate`/`api:check` (M11A task §4) to
generate TypeScript types from the real, current API contract without
requiring a live PostgreSQL connection: `create_app` never opens a database
connection while building the schema (only its lifespan does, and only a
non-blocking readiness probe that is never triggered by `app.openapi()`), so
this script passes a placeholder DSN that is never dialed.

Usage:
    python scripts/export_openapi_schema.py [output_path]

Defaults to writing `frontend/openapi/openapi.json`. Never prints or writes
a real DSN, credential, tenant id or hostname.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "frontend" / "openapi" / "openapi.json"

sys.path.insert(0, str(REPO_ROOT / "src"))


def export_schema(output_path: Path) -> None:
    from ap_agent.api.app import create_app
    from ap_agent.api.config import ApiConfig

    app = create_app(
        dsn="postgresql://schema-export-only:unused@127.0.0.1:1/schema_export_only",
        api_config=ApiConfig(enable_review_command_writes=False),
    )
    schema = app.openapi()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote OpenAPI schema ({len(schema.get('paths', {}))} paths) to {output_path}")


def main() -> int:
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    export_schema(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

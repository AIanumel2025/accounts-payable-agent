"""M8D unit test: `psycopg`/`psycopg_pool` are never imported as a side
effect of importing any Phase 7 memory module (CLAUDE.md: "No heavy or
optional dependency is imported at module import time"; the M8 task brief
extends this to `psycopg`, an optional `postgres` extra).

Runs in a subprocess with a meta-path finder that raises `ImportError` for
`psycopg`/`psycopg_pool`, so it verifies this even when the real packages
are installed in the test environment (as they must be for
`tests/unit/test_memory_config.py`'s `inspect_transport` tests, which do
call into `psycopg.conninfo` explicitly).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

_MODULES = (
    "ap_agent",
    "ap_agent.db",
    "ap_agent.db.connection",
    "ap_agent.db.migration_runner",
    "ap_agent.db.migration_manifest",
    "ap_agent.db.roles",
    "ap_agent.repositories.memory_repository",
    "ap_agent.repositories.mapping",
    "ap_agent.repositories.postgres_memory_repository",
    "ap_agent.services.memory_service",
    "ap_agent.models.memory",
    "ap_agent.config.postgres",
    "ap_agent.serialization.memory_json",
)

_SCRIPT = textwrap.dedent(
    """
    import importlib
    import importlib.abc
    import sys

    class BlockPsycopg(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "psycopg" or name.startswith("psycopg.") or name == "psycopg_pool":
                raise ImportError(f"{name} blocked for this test")
            return None

    sys.meta_path.insert(0, BlockPsycopg())

    for module_name in %r:
        importlib.import_module(module_name)

    print("OK")
    """
)


def test_memory_modules_import_without_psycopg_installed():
    script = _SCRIPT % (_MODULES,)

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd="src",
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout

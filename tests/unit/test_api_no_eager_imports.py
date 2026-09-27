"""M10 unit test: importing `ap_agent.api` (and every module it imports at
module scope) never imports `psycopg`/`psycopg_pool` or any OCR/heavy
dependency as a side effect (task §13: "no eager PaddleOCR/model loading
at API startup"; "no eager import of heavyweight OCR dependencies for
review-only API routes"; CLAUDE.md: "No heavy or optional dependency is
imported at module import time"). Mirrors
`tests/unit/test_memory_no_eager_psycopg_import.py`'s subprocess-based
approach so it holds even when the real packages are installed."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

_MODULES = (
    "ap_agent.api",
    "ap_agent.api.app",
    "ap_agent.api.config",
    "ap_agent.api.dependencies",
    "ap_agent.api.errors",
    "ap_agent.api.schemas",
    "ap_agent.api.routes.health",
    "ap_agent.api.routes.dashboard",
    "ap_agent.api.routes.review_cases",
    "ap_agent.api.routes.review_commands",
    "ap_agent.repositories.review_repository",
    "ap_agent.services.review_queries",
    "ap_agent.services.review_commands",
    "ap_agent.services.review_decisions",
    "ap_agent.services.workflow_resume",
    "ap_agent.models.interface",
)

_BLOCKED_MODULE_PREFIXES = (
    "psycopg",
    "psycopg_pool",
    "paddle",
    "paddlex",
    "paddleocr",
    "pytesseract",
    "fitz",  # PyMuPDF
    "cv2",
    "pandas",
    "IPython",
)

_SCRIPT = textwrap.dedent(
    """
    import importlib
    import importlib.abc
    import sys

    class BlockHeavyDependencies(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            for prefix in %r:
                if name == prefix or name.startswith(prefix + "."):
                    raise ImportError(f"{name} blocked for this test")
            return None

    sys.meta_path.insert(0, BlockHeavyDependencies())

    for module_name in %r:
        importlib.import_module(module_name)

    print("OK")
    """
)


def test_api_modules_import_without_psycopg_or_ocr_dependencies_installed():
    script = _SCRIPT % (_BLOCKED_MODULE_PREFIXES, _MODULES)

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd="src",
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout

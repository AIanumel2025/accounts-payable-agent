"""Cross-cutting M2 checks: package foundation, import hygiene and the
fixture/production separation required by decisions D-2 and D-3.
"""

import ast
import importlib
import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "ap_agent"

M2_MODULES = [
    "ap_agent",
    "ap_agent.exceptions",
    "ap_agent.config.settings",
    "ap_agent.models.common",
    "ap_agent.models.ingestion",
    "ap_agent.models.preprocessing",
    "ap_agent.models.ocr",
    "ap_agent.models.normalization",
    "ap_agent.models.validation",
]

# Notebook cells import these heavily-weighted, optional runtime
# dependencies. None of them may be imported as a side effect of importing
# an M2 contract module (cross-cutting invariant, §10.6 of the
# modularisation map).
FORBIDDEN_RUNTIME_IMPORTS = {
    "pandas",
    "matplotlib",
    "IPython",
    "paddle",
    "paddleocr",
    "pytesseract",
    "pymupdf",
    "cv2",
    "numpy",
    "PIL",
}

# D-3: runtime code must never name a fixture file, an expected anchor or
# an expected total. These are the four committed fixtures (§0.1 of the
# modularisation map) and a sample of the recorded totals/anchors (§10).
FORBIDDEN_FIXTURE_STRINGS = [
    "08181_flat_document.png",
    "08181_warped_document_perspective_shadow.jpg",
    "Template1_Instance90.jpg",
    "invoice_Aaron Bergman_36258.pdf",
    "TAXINVOICE",
    "873.58",
    "69.22",
    "50.10",
    "308044",
    "36258",
    "9.19",
    "882.77",
]


@pytest.mark.parametrize("module_name", M2_MODULES)
def test_m2_module_imports_successfully(module_name):
    module = importlib.import_module(module_name)
    assert module is not None


def test_m2_modules_do_not_import_heavy_optional_dependencies():
    before = set(sys.modules)
    for module_name in M2_MODULES:
        importlib.import_module(module_name)
    after = set(sys.modules)
    newly_imported = after - before

    leaked = {
        name
        for name in newly_imported
        if name.split(".")[0] in FORBIDDEN_RUNTIME_IMPORTS
    }
    assert not leaked, f"M2 contract modules pulled in heavy dependencies: {leaked}"


def _iter_production_source_files():
    return sorted(SRC_ROOT.rglob("*.py"))


def test_no_production_module_is_syntactically_broken():
    for path in _iter_production_source_files():
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))


@pytest.mark.parametrize("forbidden", FORBIDDEN_FIXTURE_STRINGS)
def test_no_production_module_contains_fixture_names_or_expected_values(forbidden):
    offenders = [
        str(path.relative_to(SRC_ROOT))
        for path in _iter_production_source_files()
        if forbidden in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{forbidden!r} must not appear in production code (D-2/D-3), "
        f"found in: {offenders}"
    )


def test_no_production_module_hardcodes_a_document_count():
    """D-2: runtime code must not assume there are exactly four documents."""

    offenders = []
    for path in _iter_production_source_files():
        source = path.read_text(encoding="utf-8")
        if "== 4" in source or "len(test_results) == 7" in source:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert not offenders, f"found a hardcoded document count in: {offenders}"

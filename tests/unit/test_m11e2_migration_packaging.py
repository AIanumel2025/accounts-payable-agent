"""M11E.2 (real AWS finding): the built wheel must carry every migration SQL file.

The first real migration invocation failed with FileNotFoundError for
`.../site-packages/ap_agent/db/migrations/0001_memory_schema_bootstrap.sql`: the SQL files were not package data, and pytest's
`pythonpath = ["src"]` (and editable installs) read them from the source tree, masking the defect. These tests build the wheel,
inspect it, install it into an isolated directory and run the same check the migration image runs
(`scripts/check_installed_migrations.py`) against the *installed* copy."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from ap_agent.db.migration_manifest import MIGRATION_MANIFEST

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
CHECK = REPO / "scripts" / "check_installed_migrations.py"
SQL_NAMES = [entry.sql_filename for entry in MIGRATION_MANIFEST]


def _run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=600, check=False, **kwargs)


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> Path:
    # Build from a pristine copy so no build/ or egg-info directory is left in the repository.
    work = tmp_path_factory.mktemp("wheel-src")
    shutil.copy(REPO / "pyproject.toml", work)
    shutil.copy(REPO / "README.md", work)
    shutil.copytree(REPO / "src", work / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "*.pyc"))
    out = tmp_path_factory.mktemp("wheel-out")
    result = _run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "-q", "-w", str(out)], cwd=work)

    assert result.returncode == 0, result.stdout + result.stderr
    (path,) = out.glob("ap_agent-*.whl")
    return path


@pytest.fixture()
def installed(wheel, tmp_path) -> Path:
    target = tmp_path / "site-packages"
    result = _run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", "--target", str(target), "-q", str(wheel)])

    assert result.returncode == 0, result.stdout + result.stderr
    return target


def _check(installed: Path, tmp_path: Path, *extra: str):
    # Fresh interpreter, cwd outside the repository, the installed copy first on the path: nothing can fall back to src/.
    environment = {**os.environ, "PYTHONPATH": str(installed)}
    environment.pop("PYTHONSTARTUP", None)
    return _run([sys.executable, str(CHECK), "--expect-prefix", str(installed), *extra], cwd=tmp_path, env=environment)


def test_the_manifest_references_five_sql_files():
    assert len(SQL_NAMES) == 5 and len(set(SQL_NAMES)) == 5
    assert all((REPO / "src" / "ap_agent" / "db" / "migrations" / name).is_file() for name in SQL_NAMES)


def test_the_wheel_contains_every_manifest_referenced_sql_file_unchanged(wheel):
    archive = zipfile.ZipFile(wheel)
    packaged = {name for name in archive.namelist() if name.endswith(".sql")}

    assert packaged == {f"ap_agent/db/migrations/{name}" for name in SQL_NAMES}

    for name in SQL_NAMES:  # byte-for-byte the repository's files: packaging must not alter a migration
        assert archive.read(f"ap_agent/db/migrations/{name}") == (REPO / "src" / "ap_agent" / "db" / "migrations" / name).read_bytes()


def test_the_installed_package_loads_all_five_migrations_with_manifest_checksums(installed, tmp_path):
    result = _check(installed, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: 5 migrations loaded" in result.stdout and str(installed) in result.stdout
    assert [line.split()[0] for line in result.stdout.splitlines()[1:]] == [entry.migration_id for entry in MIGRATION_MANIFEST]


@pytest.mark.parametrize("missing", SQL_NAMES)
def test_the_check_fails_if_any_one_sql_file_is_absent(installed, tmp_path, missing):
    (installed / "ap_agent" / "db" / "migrations" / missing).unlink()

    result = _check(installed, tmp_path)

    assert result.returncode == 1
    assert "FAIL" in result.stderr and ("FileNotFoundError" in result.stderr or missing in result.stderr)


def test_the_check_fails_if_a_sql_file_is_altered(installed, tmp_path):
    path = installed / "ap_agent" / "db" / "migrations" / SQL_NAMES[2]
    path.write_text(path.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8")
    # A trailing comment inside the last statement block changes the canonical SQL, hence the checksum.

    result = _check(installed, tmp_path)

    assert result.returncode == 1 and "FAIL" in result.stderr


def test_the_check_fails_if_the_package_was_not_imported_from_the_expected_location(installed, tmp_path):
    result = _run([sys.executable, str(CHECK), "--expect-prefix", str(tmp_path / "elsewhere")], cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(installed)})

    assert result.returncode == 2 and "expected below" in result.stderr


def test_a_source_tree_install_would_have_masked_the_defect():
    # Documents why the old tests passed: the source tree always has the files, so only an installed copy proves packaging.
    result = _run([sys.executable, str(CHECK), "--require-installed"], cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO / "src")})

    assert result.returncode == 2 and "not from an installed package" in result.stderr

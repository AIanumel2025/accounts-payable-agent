"""M11E: the image secret scan flags real credentials and ignores harmless look-alikes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scan_secrets.sh"


def _scan(tmp_path: Path, content: str) -> int:
    (tmp_path / "sample.py").write_text(content)
    return subprocess.run(["sh", str(SCRIPT), str(tmp_path)], capture_output=True, text=True).returncode


@pytest.mark.parametrize(
    "content",
    [
        "# Matches the password in a `postgresql://user:PASSWORD@host/db` DSN\n",
        'uri = f"postgresql://{user}:{password}@{host}/{db}"\n',
        'dsn = "postgresql://schema-export-only:unused@127.0.0.1:1/schema_export_only"\n',
        'dsn = "postgresql://u:pw@db.example.test/ap"  # two-character placeholder\n',
    ],
)
def test_harmless_look_alikes_are_not_findings(tmp_path, content):
    assert _scan(tmp_path, content) == 0


@pytest.mark.parametrize(
    "content",
    [
        'dsn = "postgresql://owner:Zk93hQ7vLp@db.region.aws.neon.tech/ap"\n',
        'dsn = "postgres://app_user:hunter2hunter2@10.0.0.5:5432/ap"\n',
        'key = "sk_live_abcdEFGH1234ijkl"\n',
        "-----BEGIN RSA PRIVATE KEY-----\n",
        'k = "AKIAABCDEFGHIJKLMNOP"\n',
    ],
)
def test_real_credential_shapes_are_findings(tmp_path, content):
    assert _scan(tmp_path, content) == 1


def test_application_source_and_scripts_are_clean():
    root = SCRIPT.parents[1]
    assert subprocess.run(["sh", str(SCRIPT), str(root / "src"), str(root / "scripts")]).returncode == 0

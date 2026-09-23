"""Integrity checks for the controlled invoice fixture manifest."""

import hashlib
import json
from pathlib import Path

FIXTURE_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
)
MANIFEST_PATH = FIXTURE_DIRECTORY / "manifest.json"
EXPECTED_FIXTURE_COUNT = 4


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_declares_expected_fixture_count():
    manifest = load_manifest()

    assert len(manifest["fixtures"]) == EXPECTED_FIXTURE_COUNT
    assert manifest["fixture_count"] == len(manifest["fixtures"])


def test_manifest_entries_are_unique():
    fixtures = load_manifest()["fixtures"]

    filenames = [fixture["filename"] for fixture in fixtures]
    hashes = [fixture["sha256"] for fixture in fixtures]

    assert len(filenames) == len(set(filenames))
    assert len(hashes) == len(set(hashes))
    assert filenames == sorted(filenames)


def test_declared_files_match_hash_and_size():
    for fixture in load_manifest()["fixtures"]:
        fixture_path = FIXTURE_DIRECTORY / fixture["filename"]

        assert fixture_path.is_file(), fixture["filename"]

        content = fixture_path.read_bytes()

        assert hashlib.sha256(content).hexdigest() == fixture["sha256"]
        assert len(content) == fixture["size_bytes"]
        assert fixture_path.suffix.lower() == fixture["file_extension"]

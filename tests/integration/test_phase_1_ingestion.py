"""Integration tests: Phase 1 ingestion against the four controlled
invoice fixtures.

Fixture filenames and hashes are read from
`tests/fixtures/invoices/manifest.json`, never hardcoded (per CLAUDE.md and
decisions D-2/D-3). Expected identities and dispositions are read from
`tests/golden/phase_1_expected_results.json`, the parity baseline captured
against the notebook's recorded Phase 1 outputs
(`docs/modularisation_map.md` §10.1). All writes go under `tmp_path`.
"""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from ap_agent.config.settings import IngestionConfig
from ap_agent.exceptions import IngestionValidationError
from ap_agent.models.ingestion import IngestionDisposition, IngestionErrorCode
from ap_agent.models.common import ProcessingStatus
from ap_agent.tools.ingestion import (
    calculate_sha256,
    create_batch_id,
    ingest_document,
)

FIXTURE_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "fixtures" / "invoices"
)
MANIFEST_PATH = FIXTURE_DIRECTORY / "manifest.json"
GOLDEN_PATH = (
    Path(__file__).resolve().parents[1] / "golden" / "phase_1_expected_results.json"
)


def load_manifest_fixtures() -> list[dict]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return manifest["fixtures"]


def load_golden_baseline() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def fixtures() -> list[dict]:
    return load_manifest_fixtures()


@pytest.fixture()
def golden() -> dict:
    return load_golden_baseline()


def golden_document_for(golden: dict, filename: str) -> dict:
    for document in golden["documents"]:
        if document["filename"] == filename:
            return document
    raise AssertionError(f"no golden entry for {filename!r}")


@pytest.fixture()
def ingestion_config(tmp_path) -> IngestionConfig:
    return IngestionConfig(artifact_root=tmp_path / "artifacts")


# --- per-fixture-format ingestion -----------------------------------------


def test_each_fixture_format_ingests_and_matches_the_golden_baseline(
    fixtures, golden, ingestion_config
):
    batch_id = create_batch_id(
        golden["batch"]["source"], golden["batch"]["external_reference"]
    )
    assert str(batch_id) == golden["batch"]["expected_batch_id"]

    known_hashes: set[str] = set()

    for fixture in fixtures:
        source_path = FIXTURE_DIRECTORY / fixture["filename"]
        expected = golden_document_for(golden, fixture["filename"])

        result = ingest_document(
            file_path=source_path,
            batch_id=batch_id,
            ingestion_source="integration-test",
            known_sha256_values=known_hashes,
            config=ingestion_config,
        )
        known_hashes.add(result.identity.sha256)

        assert result.identity.sha256 == fixture["sha256"]
        assert result.inspection.media_type == fixture["media_type"]
        assert result.inspection.file_size_bytes == fixture["size_bytes"]
        assert str(result.identity.content_id) == expected["expected_content_id"]
        assert str(result.identity.document_id) == expected["expected_document_id"]
        assert result.disposition.value == expected["expected_disposition"]
        assert result.event.status.value == expected["expected_event_status"]
        assert result.document.status.value == expected["expected_document_status"]
        assert result.original_preserved == expected["expected_original_preserved"]

        relative_path = result.stored_path.relative_to(
            ingestion_config.artifact_root.resolve()
        )
        assert relative_path.as_posix() == expected["expected_artifact_relative_path"]


def test_fixture_document_ids_are_unique_across_documents(fixtures, ingestion_config):
    batch_id = uuid4()
    known_hashes: set[str] = set()
    document_ids = set()
    content_ids = set()

    for fixture in fixtures:
        result = ingest_document(
            file_path=FIXTURE_DIRECTORY / fixture["filename"],
            batch_id=batch_id,
            ingestion_source="integration-test",
            known_sha256_values=known_hashes,
            config=ingestion_config,
        )
        known_hashes.add(result.identity.sha256)
        document_ids.add(result.identity.document_id)
        content_ids.add(result.identity.content_id)

    assert len(document_ids) == len(fixtures)
    assert len(content_ids) == len(fixtures)


# --- immutable preservation / byte and hash equality -----------------------


def test_original_is_preserved_byte_for_byte_for_every_fixture(
    fixtures, ingestion_config
):
    batch_id = uuid4()
    known_hashes: set[str] = set()

    for fixture in fixtures:
        source_path = FIXTURE_DIRECTORY / fixture["filename"]
        result = ingest_document(
            file_path=source_path,
            batch_id=batch_id,
            ingestion_source="integration-test",
            known_sha256_values=known_hashes,
            config=ingestion_config,
        )
        known_hashes.add(result.identity.sha256)

        assert result.stored_path.read_bytes() == source_path.read_bytes()
        assert calculate_sha256(result.stored_path) == result.identity.sha256
        assert calculate_sha256(result.stored_path) == fixture["sha256"]


def test_repeated_ingestion_of_the_same_fixture_is_idempotent(
    fixtures, ingestion_config
):
    fixture = fixtures[0]
    source_path = FIXTURE_DIRECTORY / fixture["filename"]
    batch_id = uuid4()

    first = ingest_document(
        file_path=source_path,
        batch_id=batch_id,
        ingestion_source="integration-test",
        known_sha256_values=set(),
        config=ingestion_config,
    )
    second = ingest_document(
        file_path=source_path,
        batch_id=batch_id,
        ingestion_source="integration-test",
        known_sha256_values={first.identity.sha256},
        config=ingestion_config,
    )

    assert first.stored_path == second.stored_path
    assert first.stored_path.read_bytes() == source_path.read_bytes()
    assert first.original_preserved is True
    assert second.original_preserved is False
    assert second.disposition == IngestionDisposition.DUPLICATE


def test_renamed_exact_duplicate_of_a_real_fixture_is_detected(
    fixtures, ingestion_config, tmp_path
):
    """Create a renamed byte-identical copy dynamically (never committed;
    CLAUDE.md/§7 of the modularisation map require this)."""
    fixture = fixtures[0]
    source_path = FIXTURE_DIRECTORY / fixture["filename"]

    renamed_directory = tmp_path / "incoming"
    renamed_directory.mkdir()
    renamed_path = renamed_directory / f"renamed_copy{source_path.suffix}"
    renamed_path.write_bytes(source_path.read_bytes())

    batch_id = uuid4()
    original_result = ingest_document(
        file_path=source_path,
        batch_id=batch_id,
        ingestion_source="integration-test",
        known_sha256_values=set(),
        config=ingestion_config,
    )
    duplicate_result = ingest_document(
        file_path=renamed_path,
        batch_id=batch_id,
        ingestion_source="integration-test",
        known_sha256_values={original_result.identity.sha256},
        config=ingestion_config,
    )

    assert duplicate_result.disposition == IngestionDisposition.DUPLICATE
    assert duplicate_result.event.status == ProcessingStatus.SKIPPED
    assert duplicate_result.original_preserved is False
    assert duplicate_result.identity.content_id == original_result.identity.content_id
    assert duplicate_result.identity.document_id == original_result.identity.document_id
    assert duplicate_result.stored_path == original_result.stored_path


# --- error and review routing ----------------------------------------------


def test_error_routing_for_missing_empty_and_unsupported_documents(
    ingestion_config, tmp_path
):
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    empty_path = incoming / "empty.pdf"
    empty_path.write_bytes(b"")

    unsupported_path = incoming / "notes.txt"
    unsupported_path.write_bytes(b"not an invoice")

    missing_path = incoming / "does_not_exist.pdf"

    cases = [
        (empty_path, IngestionErrorCode.EMPTY_FILE),
        (unsupported_path, IngestionErrorCode.UNSUPPORTED_EXTENSION),
        (missing_path, IngestionErrorCode.FILE_NOT_FOUND),
    ]

    batch_id = uuid4()
    for path, expected_code in cases:
        with pytest.raises(IngestionValidationError) as excinfo:
            ingest_document(
                file_path=path,
                batch_id=batch_id,
                ingestion_source="integration-test",
                known_sha256_values=set(),
                config=ingestion_config,
            )
        assert excinfo.value.code == expected_code


def test_rejected_documents_do_not_block_accepted_ones_in_the_same_batch(
    fixtures, ingestion_config, tmp_path
):
    """Fail-closed routing: a rejected document raises and is skipped by the
    caller, while every other document in the same batch still ingests."""
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    empty_path = incoming / "empty.pdf"
    empty_path.write_bytes(b"")

    batch_id = uuid4()
    known_hashes: set[str] = set()
    accepted = []
    rejected = []

    for fixture in fixtures:
        result = ingest_document(
            file_path=FIXTURE_DIRECTORY / fixture["filename"],
            batch_id=batch_id,
            ingestion_source="integration-test",
            known_sha256_values=known_hashes,
            config=ingestion_config,
        )
        known_hashes.add(result.identity.sha256)
        accepted.append(result)

    try:
        ingest_document(
            file_path=empty_path,
            batch_id=batch_id,
            ingestion_source="integration-test",
            known_sha256_values=known_hashes,
            config=ingestion_config,
        )
    except IngestionValidationError as error:
        rejected.append(error)

    assert len(accepted) == len(fixtures)
    assert all(r.disposition == IngestionDisposition.ACCEPTED for r in accepted)
    assert len(rejected) == 1
    assert rejected[0].code == IngestionErrorCode.EMPTY_FILE


# --- batch processing and isolation -----------------------------------------


def test_batch_ingestion_with_more_than_four_generated_inputs(
    fixtures, ingestion_config, tmp_path
):
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    # More synthetic documents than the four committed fixtures, to prove
    # production code handles an arbitrary iterable, not exactly four (D-2).
    synthetic_paths = []
    for index in range(3):
        path = incoming / f"synthetic_invoice_{index}.pdf"
        path.write_bytes(
            f"%PDF-1.4\nSynthetic invoice {index}\n%%EOF".encode()
        )
        synthetic_paths.append(path)

    all_paths = [FIXTURE_DIRECTORY / f["filename"] for f in fixtures] + synthetic_paths
    assert len(all_paths) > 4

    batch_id = uuid4()
    known_hashes: set[str] = set()
    results = []

    for path in all_paths:
        result = ingest_document(
            file_path=path,
            batch_id=batch_id,
            ingestion_source="integration-test",
            known_sha256_values=known_hashes,
            config=ingestion_config,
        )
        known_hashes.add(result.identity.sha256)
        results.append(result)

    assert len(results) == len(all_paths)
    assert all(r.disposition == IngestionDisposition.ACCEPTED for r in results)
    assert len({r.identity.document_id for r in results}) == len(all_paths)


def test_cross_document_isolation_in_the_artifact_tree(fixtures, ingestion_config):
    batch_id = uuid4()
    known_hashes: set[str] = set()
    results = []

    for fixture in fixtures:
        result = ingest_document(
            file_path=FIXTURE_DIRECTORY / fixture["filename"],
            batch_id=batch_id,
            ingestion_source="integration-test",
            known_sha256_values=known_hashes,
            config=ingestion_config,
        )
        known_hashes.add(result.identity.sha256)
        results.append(result)

    # Each document's artifact directory is named after its own content_id
    # and contains no other document's identifiers.
    for result in results:
        directory_name = result.stored_path.parent.name
        assert directory_name == str(result.identity.content_id)
        other_content_ids = {
            str(other.identity.content_id)
            for other in results
            if other is not result
        }
        assert directory_name not in other_content_ids


def test_no_writes_occur_outside_the_configured_artifact_root(
    fixtures, ingestion_config, tmp_path
):
    before = {p for p in tmp_path.rglob("*") if p.is_file()}

    batch_id = uuid4()
    known_hashes: set[str] = set()
    for fixture in fixtures:
        result = ingest_document(
            file_path=FIXTURE_DIRECTORY / fixture["filename"],
            batch_id=batch_id,
            ingestion_source="integration-test",
            known_sha256_values=known_hashes,
            config=ingestion_config,
        )
        known_hashes.add(result.identity.sha256)

    after = {p for p in tmp_path.rglob("*") if p.is_file()}
    new_files = after - before
    assert new_files, "expected preserved originals to be written"
    for new_file in new_files:
        assert ingestion_config.artifact_root in new_file.resolve().parents

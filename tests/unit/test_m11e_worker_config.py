"""M11E: hosted worker configuration, OCR start-up and scratch cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from ap_agent.config.deployment import HostedConfigurationError, StorageMode
from ap_agent.worker.__main__ import EXIT_CONFIGURATION, EXIT_OCR_UNAVAILABLE, _ScratchCleaningExecutor, main
from ap_agent.worker.config import load_worker_config

pytestmark = pytest.mark.unit

REFERENCE = str(Path(__file__).resolve().parents[1] / "fixtures" / "reference_data")

HOSTED = {
    "AP_AGENT_ENVIRONMENT": "hosted",
    "AP_AGENT_ENABLE_WORKER_EXECUTION": "true",
    "AP_AGENT_ARTIFACT_STORAGE": "s3",
    "AP_AGENT_S3_ENDPOINT_URL": "https://objects.example.test",
    "AP_AGENT_S3_BUCKET": "example-bucket",
    "AP_AGENT_S3_ACCESS_KEY_ID": "AKIAEXAMPLEEXAMPLE",
    "AP_AGENT_S3_SECRET_ACCESS_KEY": "super-secret-value",
    "AP_AGENT_REFERENCE_DATA_DIRECTORY": REFERENCE,
    "AP_AGENT_POSTGRES_DSN": "postgresql://runtime:pw@db.example.test/ap?sslmode=require",
}


def _problems(**overrides):
    environment = {**HOSTED, **overrides}
    environment = {k: v for k, v in environment.items() if v is not None}

    with pytest.raises(HostedConfigurationError) as caught:
        load_worker_config(environment)

    return caught.value.problems


def test_hosted_worker_loads_with_object_storage_and_needs_no_artifact_root():
    config = load_worker_config(HOSTED)
    assert config.hosted and config.storage_mode is StorageMode.S3
    assert config.artifact_root is None and config.ocr_provider == "paddleocr"
    assert "super-secret-value" not in repr(config)


def test_hosted_worker_refuses_local_storage():
    problems = _problems(AP_AGENT_ARTIFACT_STORAGE="local", AP_AGENT_ARTIFACT_ROOT="/tmp/x")
    assert "HOSTED_REQUIRES_OBJECT_STORAGE" in problems and "HOSTED_FORBIDS_LOCAL_ARTIFACT_ROOT" in problems


def test_hosted_worker_requires_the_real_ocr_provider():
    assert "HOSTED_REQUIRES_OCR_PROVIDER_PADDLEOCR" in _problems(AP_AGENT_WORKER_OCR_PROVIDER="tesseract")


def test_hosted_worker_is_never_scoped_to_one_tenant():
    assert "HOSTED_FORBIDS_WORKER_TENANT_SCOPE" in _problems(AP_AGENT_WORKER_TENANT_ID="11111111-1111-1111-1111-111111111111")


def test_local_worker_configuration_is_unchanged():
    config = load_worker_config({"AP_AGENT_ARTIFACT_ROOT": "/tmp/a", "AP_AGENT_REFERENCE_DATA_DIRECTORY": REFERENCE})
    assert not config.hosted and config.storage_mode is StorageMode.LOCAL and config.artifact_root == Path("/tmp/a")


def test_a_hosted_worker_whose_ocr_provider_is_unavailable_exits_before_claiming_anything(monkeypatch, capsys):
    for name, value in HOSTED.items():
        monkeypatch.setenv(name, value)

    def broken(self):
        raise ImportError("paddle is not installed")

    monkeypatch.setattr("ap_agent.worker.pipeline.OcrEngineProvider.get", broken)

    claimed = []
    monkeypatch.setattr(
        "ap_agent.repositories.operations_repository.OperationsRepository.claim_next_job",
        lambda self, **kwargs: claimed.append(1),
    )

    assert main(["--once"]) == EXIT_OCR_UNAVAILABLE
    output = capsys.readouterr()
    assert "OCR provider could not be initialised" in output.err
    assert claimed == []
    assert "super-secret-value" not in output.out + output.err and "db.example.test" not in output.out + output.err
    assert "AP_AGENT_S3_SECRET_ACCESS_KEY=set" in output.out  # status names, never values


def test_invalid_hosted_configuration_exits_with_codes_not_values(monkeypatch, capsys):
    for name, value in {**HOSTED, "AP_AGENT_ARTIFACT_STORAGE": "local"}.items():
        monkeypatch.setenv(name, value)

    assert main(["--once"]) == EXIT_CONFIGURATION
    assert "HOSTED_REQUIRES_OBJECT_STORAGE" in capsys.readouterr().err


class _Inner:
    def __init__(self, fail: bool) -> None:
        self.fail = fail

    def execute(self, job):
        if self.fail:
            raise RuntimeError("boom")


@pytest.mark.parametrize("fail", [False, True])
def test_scratch_directory_is_removed_after_success_and_failure(tmp_path, fail):
    scratch = tmp_path / "job"
    (scratch / "phase1").mkdir(parents=True)
    (scratch / "phase1" / "page.png").write_bytes(b"x")
    executor = _ScratchCleaningExecutor(_Inner(fail), lambda job: scratch)

    if fail:
        with pytest.raises(RuntimeError):
            executor.execute(object())
    else:
        executor.execute(object())

    assert not scratch.exists()

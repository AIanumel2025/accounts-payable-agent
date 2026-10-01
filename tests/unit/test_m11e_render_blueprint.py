"""M11E: the Render Blueprint satisfies the deployment rules."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("verify_render_blueprint", REPO / "scripts" / "verify_render_blueprint.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def _problems(mutate=None) -> list[str]:
    document = yaml.safe_load((REPO / "render.yaml").read_text())

    if mutate:
        mutate(document)

    path = REPO / ".blueprint-under-test.yaml"
    path.write_text(yaml.safe_dump(document))

    try:
        return verifier.verify(path)
    finally:
        path.unlink(missing_ok=True)


def _service(document, kind):
    return next(service for service in document["services"] if service["type"] == kind)


def test_checked_in_blueprint_is_valid():
    assert _problems() == []


def test_blueprint_commits_no_secret_and_exposes_nothing_public_but_the_web_service():
    text = (REPO / "render.yaml").read_text()
    for forbidden in ("postgresql://", "sk_live_", "sk_test_", "pk_live_", "pk_test_", "BEGIN PRIVATE", "AKIA"):
        assert forbidden not in text
    document = yaml.safe_load(text)
    assert [s["type"] for s in document["services"]].count("web") == 1


@pytest.mark.parametrize(
    ("mutation", "fragment"),
    [
        (lambda d: _service(d, "worker").update(numInstances=2), "numInstances"),
        (lambda d: _service(d, "web").update(type="pserv"), "expected exactly 1 web"),
        (lambda d: _service(d, "pserv").update(scaling={"minInstances": 1}), "scaling"),
        (lambda d: _service(d, "pserv")["envVars"].append({"key": "AP_AGENT_POSTGRES_DSN", "value": "postgresql://u:p@h/d"}), "committed value"),
        (lambda d: _service(d, "web")["envVars"].append({"key": "AP_AGENT_POSTGRES_MIGRATION_DSN", "sync": False}), "migration DSN"),
        (lambda d: _service(d, "worker").update(preDeployCommand="python scripts/migrate.py"), "pre-deploy"),
        (lambda d: _service(d, "pserv").pop("preDeployCommand"), "preDeployCommand must run"),
        (lambda d: _service(d, "pserv")["envVars"].append({"key": "CLERK_SECRET_KEY", "sync": False}), "CLERK_SECRET_KEY"),
        (lambda d: _service(d, "web")["envVars"].append({"key": "AP_AGENT_S3_BUCKET", "sync": False}), "object-storage credentials"),
        (lambda d: _service(d, "web")["envVars"].append({"key": "AP_AGENT_DEV_TENANT_ID", "value": "x"}), "AP_AGENT_DEV_"),
        (lambda d: d["services"].append({"type": "keyvalue", "name": "cache"}), "forbidden type"),
        (lambda d: _service(d, "worker").update(dockerfilePath="docker/missing.Dockerfile"), "does not exist"),
        (lambda d: _service(d, "worker").update(autoDeploy=True), "autoDeploy"),
        (lambda d: next(v for v in _service(d, "pserv")["envVars"] if v["key"] == "AP_AGENT_ARTIFACT_STORAGE").update(value="local"), "must be s3"),
    ],
)
def test_rule_violations_are_reported(mutation, fragment):
    assert any(fragment in problem for problem in _problems(mutation))

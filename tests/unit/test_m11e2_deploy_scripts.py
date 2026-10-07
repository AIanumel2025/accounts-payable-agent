"""M11E.2: deploy.sh, preflight.sh, build-and-push.sh and teardown.sh behave as specified, against stub aws/sam/docker binaries.
No AWS access, no network, no real credentials."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.support.m11e2_stubs import StubAws

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "a" * 64
REGISTRY = "111122223333.dkr.ecr.eu-west-2.amazonaws.com"
IMAGES = {
    "WEB": f"{REGISTRY}/ap-agent-production/web@{DIGEST}",
    "API": f"{REGISTRY}/ap-agent-production/api:abc123def456",
    "WORKER": f"{REGISTRY}/ap-agent-production/worker@{DIGEST}",
    "MIGRATE": f"{REGISTRY}/ap-agent-production/migrate@{DIGEST}",
}
CLERK = {"CLERK_PUBLISHABLE_KEY": "pk_test_" + "abcdefgh" * 2, "CLERK_ISSUER": "https://example.clerk.accounts.dev"}


@pytest.fixture()
def tree(tmp_path):
    """A private copy of the deploy scripts, the template and the verifier: the scripts locate the repo from their own path."""

    root = tmp_path / "repo"
    shutil.copytree(REPO / "deploy" / "aws", root / "deploy" / "aws", ignore=shutil.ignore_patterns(".local"))
    (root / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "verify_aws_templates.py", root / "scripts")
    return root


def _write_images(stub: StubAws, images=IMAGES) -> None:
    state = stub.directory / "state"
    state.mkdir(exist_ok=True)
    (state / "images.env").write_text("".join(f"{name}_IMAGE_URI={uri}\n" for name, uri in images.items()))


def _run(tree, stub, script, *args, env=None, stdin=""):
    return subprocess.run(
        ["bash", str(tree / "deploy" / "aws" / "scripts" / script), *args],
        env=stub.environment({}, **(env or {})), input=stdin, capture_output=True, text=True, timeout=120, check=False,
    )


def _preflight_rules(**overrides):
    rules = [
        {"args": ["sts", "get-caller-identity"], "out": "111122223333\n"},
        {"args": ["get-account-settings"], "out": "10\n"},
        {"args": ["service-quotas", "get-service-quota"], "out": "6.0\n"},
        {"args": ["describe-vpcs"], "out": "1\n"},
        {"args": ["cloudformation", "describe-stacks"], "rc": 255, "err": "does not exist"},
        {"args": ["ecr", "describe-images"], "out": DIGEST + "\n"},
        {"args": ["ssm", "get-parameter"], "out": "SecureString\n"},
    ]
    return [overrides[key] for key in overrides] + rules if overrides else rules


# -- deploy.sh --------------------------------------------------------------------------------------------------


def _sam_deploy(stub):
    calls = stub.calls_of("sam", "deploy")
    assert len(calls) == 1
    return calls[0]


def _parameter_overrides(call):
    return call[call.index("--parameter-overrides") + 1:]


def test_pass1_does_not_pass_an_empty_frontend_origin(tree, tmp_path):
    stub = StubAws(tmp_path, [{"args": ["sts"], "out": "111122223333\n"}])
    _write_images(stub)

    result = _run(tree, stub, "deploy.sh", "pass1", env=CLERK)

    assert result.returncode == 0, result.stderr
    overrides = _parameter_overrides(_sam_deploy(stub))
    assert not any(item.startswith("FrontendOrigin") for item in overrides)
    assert "FrontendOrigin=" not in " ".join(_sam_deploy(stub))
    assert not any(item.startswith("AlarmEmail") for item in overrides)  # an empty optional value is omitted too
    assert not any(item.startswith("WorkerMemoryMb") for item in overrides)  # the Lambda worker memory knob is gone
    assert {"OcrTaskMaxSeconds=720", "WebAndApiReservedConcurrency=-1", "Environment=production"} <= set(overrides)
    assert f"WorkerImageUri={IMAGES['WORKER']}" in overrides and f"MigrateImageUri={IMAGES['MIGRATE']}" in overrides


def test_pass2_sets_the_origin_without_a_trailing_slash(tree, tmp_path):
    stub = StubAws(tmp_path, [{"args": ["describe-stacks", "FrontendUrl"], "out": "https://abcd1234.lambda-url.eu-west-2.on.aws/\n"}])
    _write_images(stub)

    result = _run(tree, stub, "deploy.sh", "pass2", env={**CLERK, "ALARM_EMAIL": "ops@example.test"})

    assert result.returncode == 0, result.stderr
    overrides = _parameter_overrides(_sam_deploy(stub))
    assert "FrontendOrigin=https://abcd1234.lambda-url.eu-west-2.on.aws" in overrides and "AlarmEmail=ops@example.test" in overrides


def test_every_sam_image_function_has_an_explicit_repository_mapping(tree, tmp_path):
    stub = StubAws(tmp_path, [])
    _write_images(stub)
    _run(tree, stub, "deploy.sh", "pass1", env=CLERK)
    call = _sam_deploy(stub)
    mappings = [call[i + 1] for i, word in enumerate(call) if word == "--image-repositories"]

    # tag and digest are stripped: the mapping names the repository only; the dispatcher shares the migration image
    assert sorted(mappings) == sorted([
        f"ApiFunction={REGISTRY}/ap-agent-production/api",
        f"WebFunction={REGISTRY}/ap-agent-production/web",
        f"MigrateFunction={REGISTRY}/ap-agent-production/migrate",
        f"DispatcherFunction={REGISTRY}/ap-agent-production/migrate",
    ])
    assert not any("worker" in mapping for mapping in mappings)  # the worker is an ECS task, not a SAM function

    # ... and these are exactly the template's image functions
    import importlib.util

    spec = importlib.util.spec_from_file_location("v", REPO / "scripts" / "verify_aws_templates.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    functions = verifier._resources(verifier.load_template(REPO / "deploy" / "aws" / "template.yaml"), "AWS::Serverless::Function")
    assert sorted(mapping.split("=")[0] for mapping in mappings) == sorted(functions)


def test_deploy_requires_every_image_uri(tree, tmp_path):
    stub = StubAws(tmp_path, [])
    _write_images(stub, {key: value for key, value in IMAGES.items() if key != "WORKER"})

    result = _run(tree, stub, "deploy.sh", "pass1", env=CLERK)

    assert result.returncode != 0 and "WORKER_IMAGE_URI" in result.stderr and stub.calls_of("sam", "deploy") == []


def test_deploy_requires_build_first_and_a_valid_pass(tree, tmp_path):
    stub = StubAws(tmp_path, [])

    assert _run(tree, stub, "deploy.sh", "pass1", env=CLERK).returncode != 0
    assert _run(tree, stub, "deploy.sh", "pass3", env=CLERK).returncode != 0
    assert stub.calls_of("sam", "deploy") == []


def test_image_repository_helper(tree):
    script = f'source "{tree}/deploy/aws/scripts/_common.sh"; image_repository "$1"'
    run = lambda uri: subprocess.run(["bash", "-c", script, "x", uri], capture_output=True, text=True, check=True, env={"STATE_DIR": str(tree / "s"), "PATH": "/usr/bin:/bin"}).stdout

    assert run(f"{REGISTRY}/ap-agent-production/api:abc") == f"{REGISTRY}/ap-agent-production/api"
    assert run(f"{REGISTRY}/ap-agent-production/api@{DIGEST}") == f"{REGISTRY}/ap-agent-production/api"
    assert run(f"{REGISTRY}/ap-agent-production/api") == f"{REGISTRY}/ap-agent-production/api"


# -- preflight.sh ------------------------------------------------------------------------------------------------------


def test_preflight_passes_when_everything_is_ready(tree, tmp_path):
    stub = StubAws(tmp_path, _preflight_rules())
    _write_images(stub)

    result = _run(tree, stub, "preflight.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Preflight passed" in result.stdout and "3008 MB" in result.stdout
    assert "Fargate On-Demand vCPU quota: 6.0" in result.stdout
    assert "111122223333" not in result.stdout and "****3333" in result.stdout  # the account id is masked


def test_preflight_blocks_when_a_lambda_asks_for_more_than_3008_mb(tree, tmp_path):
    template = tree / "deploy" / "aws" / "template.yaml"
    template.write_text(template.read_text().replace("MemorySize: 256", "MemorySize: 8192", 1))
    stub = StubAws(tmp_path, _preflight_rules())
    _write_images(stub)

    result = _run(tree, stub, "preflight.sh")

    assert result.returncode == 1 and "BLOCK a Lambda" in result.stdout and "3008" in result.stdout


@pytest.mark.parametrize("quota,blocking", [("2.0", True), ("3.0", True), ("4.0", False), ("32.0", False)])
def test_preflight_requires_four_fargate_vcpus(tree, tmp_path, quota, blocking):
    stub = StubAws(tmp_path, _preflight_rules(quota={"args": ["service-quotas"], "out": quota + "\n"}))
    _write_images(stub)

    result = _run(tree, stub, "preflight.sh")

    assert (result.returncode == 1) is blocking
    assert ("BLOCK Fargate" in result.stdout) is blocking


def test_an_unreadable_fargate_quota_is_informational_only(tree, tmp_path):
    stub = StubAws(tmp_path, _preflight_rules(quota={"args": ["service-quotas"], "rc": 254, "err": "AccessDenied"}))
    _write_images(stub)

    result = _run(tree, stub, "preflight.sh")

    assert result.returncode == 0 and "info  could not read the Fargate" in result.stdout


@pytest.mark.parametrize("status", ["ROLLBACK_COMPLETE", "CREATE_FAILED", "UPDATE_IN_PROGRESS"])
def test_preflight_blocks_on_an_unusable_existing_stack(tree, tmp_path, status):
    stub = StubAws(tmp_path, _preflight_rules(stack={"args": ["cloudformation", "describe-stacks"], "out": status + "\n"}))
    _write_images(stub)

    result = _run(tree, stub, "preflight.sh")

    assert result.returncode == 1 and (f"is {status}" in result.stdout or f"in {status}" in result.stdout)
    if status != "UPDATE_IN_PROGRESS":
        assert "delete the STACK ONLY" in result.stdout and "keeps the ECR stack and the SSM secrets" in result.stdout


def test_preflight_accepts_an_existing_healthy_stack(tree, tmp_path):
    stub = StubAws(tmp_path, _preflight_rules(stack={"args": ["cloudformation", "describe-stacks"], "out": "UPDATE_COMPLETE\n"}))
    _write_images(stub)

    assert _run(tree, stub, "preflight.sh").returncode == 0


def test_preflight_blocks_when_an_image_is_missing_from_ecr(tree, tmp_path):
    stub = StubAws(tmp_path, _preflight_rules(image={"args": ["ecr", "describe-images", "ap-agent-production/worker"], "rc": 254, "err": "ImageNotFoundException"}))
    _write_images(stub)

    result = _run(tree, stub, "preflight.sh")

    assert result.returncode == 1 and "BLOCK image worker" in result.stdout and "ok    image web exists" in result.stdout


def test_preflight_blocks_without_recorded_images(tree, tmp_path):
    stub = StubAws(tmp_path, _preflight_rules())

    result = _run(tree, stub, "preflight.sh")

    assert result.returncode == 1 and "run build-and-push.sh first" in result.stdout


def test_preflight_checks_digest_and_tag_references(tree, tmp_path):
    stub = StubAws(tmp_path, _preflight_rules())
    _write_images(stub)
    _run(tree, stub, "preflight.sh")
    selectors = [call[call.index("--image-ids") + 1] for call in stub.calls_of("aws", "describe-images")]

    assert sorted(selectors) == sorted([f"imageDigest={DIGEST}", "imageTag=abc123def456", f"imageDigest={DIGEST}", f"imageDigest={DIGEST}"])


def test_preflight_blocks_on_each_missing_secret_without_reading_it(tree, tmp_path):
    stub = StubAws(tmp_path, _preflight_rules(secret={"args": ["ssm", "get-parameter", "clerk-secret-key"], "rc": 254, "err": "ParameterNotFound"}))
    _write_images(stub)

    result = _run(tree, stub, "preflight.sh")

    assert result.returncode == 1 and "BLOCK SSM clerk-secret-key is missing" in result.stdout
    for call in stub.calls_of("aws", "get-parameter"):
        assert "--with-decryption" not in call  # existence only: values are never read


def test_preflight_output_never_contains_a_secret_value(tree, tmp_path):
    rules = _preflight_rules(secret={"args": ["ssm"], "out": "postgresql://user:hunter2@host/db\n"})
    stub = StubAws(tmp_path, rules)
    _write_images(stub)

    result = _run(tree, stub, "preflight.sh")

    assert "hunter2" not in result.stdout + result.stderr and "postgresql://" not in result.stdout


# -- build-and-push.sh ---------------------------------------------------------------------------------------------------


def _build_rules():
    return [
        {"args": ["sts"], "out": "111122223333\n"},
        {"args": ["ecr", "describe-images"], "out": DIGEST + "\n"},
        {"args": ["ecr", "get-login-password"], "out": "token-not-printed\n"},
    ]


def test_build_and_push_records_immutable_digest_uris_and_can_rebuild_one_image(tree, tmp_path):
    stub = StubAws(tmp_path, _build_rules())
    other = {"WEB": IMAGES["WEB"], "API": IMAGES["API"], "MIGRATE": IMAGES["MIGRATE"], "WORKER": f"{REGISTRY}/ap-agent-production/worker@sha256:{'b' * 64}"}
    _write_images(stub, other)

    result = _run(tree, stub, "build-and-push.sh", "worker", env={"IMAGE_TAG": "tag123"})

    assert result.returncode == 0, result.stderr
    recorded = dict(line.split("=", 1) for line in (stub.directory / "state" / "images.env").read_text().splitlines())
    assert recorded["WORKER_IMAGE_URI"] == f"{REGISTRY}/ap-agent-production/worker@{DIGEST}"
    assert {key: recorded[key] for key in ("WEB_IMAGE_URI", "API_IMAGE_URI", "MIGRATE_IMAGE_URI")} == {f"{k}_IMAGE_URI": v for k, v in other.items() if k != "WORKER"}
    builds = stub.calls_of("docker", "build")
    assert len(builds) == 1 and "worker.lambda.Dockerfile" in " ".join(builds[0])
    assert "token-not-printed" not in result.stdout + result.stderr


def test_build_and_push_all_images_by_default_and_rejects_unknown_names(tree, tmp_path):
    stub = StubAws(tmp_path, _build_rules())

    result = _run(tree, stub, "build-and-push.sh", env={"IMAGE_TAG": "t", **CLERK})

    assert result.returncode == 0, result.stderr
    assert len(stub.calls_of("docker", "build")) == 4
    assert set(dict(line.split("=", 1) for line in (stub.directory / "state" / "images.env").read_text().splitlines())) == {f"{k}_IMAGE_URI" for k in IMAGES}
    assert _run(tree, stub, "build-and-push.sh", "bogus").returncode != 0


def test_rebuilding_only_worker_needs_no_clerk_key(tree, tmp_path):
    stub = StubAws(tmp_path, _build_rules())

    assert _run(tree, stub, "build-and-push.sh", "worker", env={"IMAGE_TAG": "t"}).returncode == 0
    assert _run(tree, stub, "build-and-push.sh", "web", env={"IMAGE_TAG": "t"}).returncode != 0


# -- teardown.sh ------------------------------------------------------------------------------------------------------------


def test_stack_only_teardown_keeps_ecr_images_and_secrets(tree, tmp_path):
    stub = StubAws(tmp_path, [{"args": ["sts"], "out": "111122223333\n"}, {"args": ["describe-stacks", "UploadBucketName"], "out": "bucket-x\n"}])

    result = _run(tree, stub, "teardown.sh", "--stack-only", stdin="production\n")

    assert result.returncode == 0, result.stdout + result.stderr
    deleted = [call[call.index("--stack-name") + 1] for call in stub.calls_of("aws", "delete-stack")]
    assert deleted == ["ap-agent-production"]
    assert stub.calls_of("aws", "delete-repository") == [] and stub.calls_of("aws", "delete-parameter") == []
    assert "ECR stack, the images and the SSM secrets were kept" in result.stdout


def test_teardown_requires_the_confirmation_and_a_known_flag(tree, tmp_path):
    stub = StubAws(tmp_path, [{"args": ["sts"], "out": "111122223333\n"}])

    assert _run(tree, stub, "teardown.sh", "--stack-only", stdin="nope\n").returncode != 0
    assert _run(tree, stub, "teardown.sh", "--everything", stdin="production\n").returncode != 0
    assert stub.calls_of("aws", "delete-stack") == []


def test_full_teardown_still_removes_ecr_and_secrets(tree, tmp_path):
    stub = StubAws(tmp_path, [{"args": ["sts"], "out": "111122223333\n"}])

    result = _run(tree, stub, "teardown.sh", stdin="production\nn\n")

    assert len(stub.calls_of("aws", "delete-stack")) == 2 and stub.calls_of("aws", "delete-parameter") and stub.calls_of("aws", "delete-repository")
    assert result.returncode in (0, 1)  # verify-teardown's answer depends on the stub; the deletions are what matter


# -- shell hygiene -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("script", sorted(p.name for p in (REPO / "deploy" / "aws" / "scripts").glob("*.sh")))
def test_every_script_parses_and_prints_no_credentials(script):
    path = REPO / "deploy" / "aws" / "scripts" / script

    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    text = path.read_text()
    assert "--with-decryption" not in text or script == "create-secrets.sh"
    assert "AWS_SECRET_ACCESS_KEY" not in text and "aws configure set" not in text and "create-access-key" not in text


# -- invoke-migration.sh (real-AWS finding: --tenant-key was parsed as a jq option) --------------------------------------------


def _invoke(tree, tmp_path, *args):
    stub = StubAws(tmp_path, [{"args": ["lambda", "invoke"], "out": '{"StatusCode":200}\n', "write_last_arg": '{"ok": true}'}])
    result = _run(tree, stub, "invoke-migration.sh", *args)
    calls = stub.calls_of("aws", "invoke")
    payload = None

    if calls:
        import json

        payload = json.loads(calls[0][calls[0].index("--payload") + 1])

    return result, payload, stub


def test_identity_register_tenant_passes_option_like_values_as_data(tree, tmp_path):
    result, payload, _ = _invoke(tree, tmp_path, "identity", "register-tenant", "--tenant-key", "acme", "--display-name", "Acme Ltd")

    assert result.returncode == 0, result.stdout + result.stderr
    assert payload == {"action": "identity", "args": ["register-tenant", "--tenant-key", "acme", "--display-name", "Acme Ltd"]}


def test_identity_register_keeps_uuid_org_user_and_role_in_exact_order(tree, tmp_path):
    arguments = ["register", "--tenant-id", "11111111-2222-3333-4444-555555555555", "--org-id", "org_2abcDEF", "--user-id", "user_9xyz", "--role", "AP_OPERATOR"]
    result, payload, _ = _invoke(tree, tmp_path, "identity", *arguments)

    assert result.returncode == 0, result.stdout + result.stderr
    assert payload == {"action": "identity", "args": arguments}  # order and count preserved exactly


@pytest.mark.parametrize(
    "values",
    [
        ["register-tenant", "--tenant-key", "acme", "--display-name", "Acme Holdings of  Two Spaces"],
        ["register-tenant", "--tenant-key", "k", "--display-name", "  leading and trailing  "],
        ["inspect", "--tenant-id", "11111111-2222-3333-4444-555555555555", "--json"],
        ["x", "--", "--help", "-n", "--args", "--arg"],  # jq option look-alikes, including a literal `--`
        ["register-tenant", "--display-name", 'quote " backslash \\ dollar $HOME `backtick` $(id) ; & | > <'],
        ["register-tenant", "--display-name", "multi\nline"],
    ],
)
def test_identity_values_with_spaces_and_shell_metacharacters_arrive_verbatim(tree, tmp_path, values):
    result, payload, _ = _invoke(tree, tmp_path, "identity", *values)

    assert result.returncode == 0, result.stdout + result.stderr
    assert payload == {"action": "identity", "args": values}


def test_identity_arguments_are_never_interpolated_or_leaked(tree, tmp_path):
    marker = tmp_path / "pwned"
    values = ["register-tenant", "--display-name", f"$(touch {marker})", "--tenant-key", f"`touch {marker}`"]
    result, payload, stub = _invoke(tree, tmp_path, "identity", *values)

    assert result.returncode == 0 and payload["args"] == values
    assert not marker.exists()  # nothing was evaluated by a shell
    assert "AWS_SECRET" not in result.stdout + result.stderr and "--with-decryption" not in " ".join(sum(stub.calls(), []))


def test_identity_needs_a_sub_command_and_migrate_payload_is_unchanged(tree, tmp_path):
    result, payload, _ = _invoke(tree, tmp_path, "identity")
    assert result.returncode != 0 and payload is None

    result, payload, _ = _invoke(tree, tmp_path, "migrate")
    assert result.returncode == 0 and payload == {"action": "migrate"}


def test_a_failed_task_result_fails_the_script(tree, tmp_path):
    stub = StubAws(tmp_path, [{"args": ["lambda", "invoke"], "out": "{}\n", "write_last_arg": '{"ok": false, "error": "X"}'}])

    assert _run(tree, stub, "invoke-migration.sh", "migrate").returncode != 0


# -- smoke.sh: a forged header with no session is 401 AUTHENTICATION_REQUIRED (403 is for a user without an organization) ----


def _smoke(tree, tmp_path, *, forged_status="401", forged_body='{"errors":["AUTHENTICATION_REQUIRED"],"generated_at":"x"}'):
    rules = [
        {"args": ["describe-stacks", "FrontendUrl"], "out": "https://front.example.test/\n"},
        {"args": ["describe-stacks", "ApiUrl"], "out": "https://api.example.test/\n"},
        {"args": ["describe-stacks", "UploadBucketName"], "out": "bucket-x\n"},
        {"args": ["upload-intents"], "tool": "curl", "out": forged_status, "body": forged_body},
        {"args": [], "tool": "curl", "out": "200"},
    ]
    stub = StubAws(tmp_path, rules)
    return _run(tree, stub, "smoke.sh"), stub


def test_the_forged_header_check_expects_401_and_the_safe_error_code(tree, tmp_path):
    result, stub = _smoke(tree, tmp_path)

    assert "ok   forged server-to-server header is refused (401, not authenticated)" in result.stdout
    assert "ok   forged header answered AUTHENTICATION_REQUIRED" in result.stdout
    assert "FAIL forged" not in result.stdout


def test_the_forged_header_check_fails_on_403_or_a_missing_error_code(tree, tmp_path):
    wrong_status, _ = _smoke(tree, tmp_path / "a", forged_status="403")
    assert "FAIL forged server-to-server header is refused (401, not authenticated) (expected 401, got 403)" in wrong_status.stdout

    missing_code, _ = _smoke(tree, tmp_path / "b", forged_body='{"errors":["SOMETHING_ELSE"]}')
    assert "FAIL forged-header response lacks AUTHENTICATION_REQUIRED" in missing_code.stdout and missing_code.returncode != 0


def test_the_forged_header_is_still_sent_and_the_script_asserts_401_not_403():
    text = (REPO / "deploy" / "aws" / "scripts" / "smoke.sh").read_text()

    assert "x-ap-agent-clerk-authorization: Bearer forged" in text and '"$forged" 401' in text
    assert 'check "forged server-to-server header is refused' in text and "403" not in text.split("forged=")[1].split("rm -f")[0]

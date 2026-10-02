"""M11E.1: SSM secret retrieval at start-up and the non-public migration task."""

from __future__ import annotations

import pytest

from ap_agent.aws import migration_handler
from ap_agent.aws.secrets import SecretLoadError, load_ssm_parameters_into_environment, parse_parameter_mapping

pytestmark = pytest.mark.unit


class FakeSsm:
    def __init__(self, values=None, fail=False):
        self.values = values or {}
        self.requested: list[tuple[str, bool]] = []
        self.fail = fail

    def get_parameter(self, *, Name, WithDecryption):
        self.requested.append((Name, WithDecryption))
        if self.fail:
            raise ConnectionError(f"denied for {Name}")
        return {"Parameter": {"Value": self.values[Name]}}


def test_parameters_are_loaded_decrypted_into_the_environment():
    env = {"AP_AGENT_SSM_PARAMETERS": "A_SECRET=/ap/a,B_SECRET=/ap/b"}
    ssm = FakeSsm({"/ap/a": "alpha\n", "/ap/b": "beta"})

    assert load_ssm_parameters_into_environment(env, client=ssm) == ("A_SECRET", "B_SECRET")
    assert env["A_SECRET"] == "alpha" and env["B_SECRET"] == "beta"
    assert all(decrypt for _, decrypt in ssm.requested)


def test_an_already_set_variable_is_not_overwritten_or_fetched():
    env = {"AP_AGENT_SSM_PARAMETERS": "A_SECRET=/ap/a", "A_SECRET": "explicit"}
    ssm = FakeSsm({"/ap/a": "from-ssm"})

    assert load_ssm_parameters_into_environment(env, client=ssm) == ()
    assert env["A_SECRET"] == "explicit" and ssm.requested == []


def test_nothing_happens_and_boto3_is_not_needed_without_a_mapping():
    assert load_ssm_parameters_into_environment({}) == ()


def test_failures_name_the_variable_never_the_path_or_value():
    env = {"AP_AGENT_SSM_PARAMETERS": "A_SECRET=/ap/very-secret-path"}

    with pytest.raises(SecretLoadError) as raised:
        load_ssm_parameters_into_environment(env, client=FakeSsm(fail=True))

    assert "A_SECRET" in str(raised.value) and "very-secret-path" not in str(raised.value)

    with pytest.raises(SecretLoadError):
        load_ssm_parameters_into_environment(env, client=FakeSsm({"/ap/very-secret-path": "  "}))


@pytest.mark.parametrize("raw", ["lowercase=/a", "A=relative", "A", "A=/x y", "=/a"])
def test_malformed_mapping_is_refused(raw):
    with pytest.raises(SecretLoadError):
        parse_parameter_mapping(raw)


def test_migration_handler_rejects_unknown_actions_and_unsafe_identity_commands():
    assert migration_handler.handler({"action": "drop-everything"}, None)["error"] == "ACTION_NOT_SUPPORTED"
    assert migration_handler.handler({}, None)["error"] == "ACTION_NOT_SUPPORTED"
    assert migration_handler.handler({"action": "identity", "args": "register"}, None)["error"] == "IDENTITY_ARGUMENTS_INVALID"
    assert migration_handler.handler({"action": "identity", "args": ["rm", "-rf"]}, None)["error"] == "IDENTITY_COMMAND_NOT_ALLOWED"
    assert migration_handler.handler({"action": "identity", "args": []}, None)["error"] == "IDENTITY_COMMAND_NOT_ALLOWED"


def test_migration_fails_closed_without_a_migration_dsn(monkeypatch):
    for name in ("AP_AGENT_POSTGRES_MIGRATION_DSN", "AP_AGENT_POSTGRES_DSN", "AP_AGENT_SSM_PARAMETERS"):
        monkeypatch.delenv(name, raising=False)

    result = migration_handler.handler({"action": "migrate"}, None)

    assert result["ok"] is False and result["error"].startswith("MIGRATION_DSN_ERROR")


def test_identity_command_runs_the_existing_cli_and_reports_a_missing_dsn(monkeypatch):
    monkeypatch.delenv("AP_AGENT_POSTGRES_MIGRATION_DSN", raising=False)

    result = migration_handler.handler({"action": "identity", "args": ["inspect", "--json"]}, None)

    assert result["ok"] is False and result["exit_code"] == 2 and "MIGRATION_DSN" in result["output"]


# -- PaddleOCR assets on a read-only image --------------------------------------------------------


def test_paddle_cache_is_a_noop_without_a_baked_directory():
    from ap_agent.aws.paddle_cache import prepare_paddlex_cache

    assert prepare_paddlex_cache({}) is None


def test_paddle_cache_links_baked_models_and_fonts_into_a_writable_directory(tmp_path):
    from ap_agent.aws.paddle_cache import prepare_paddlex_cache

    baked = tmp_path / "baked"
    (baked / "official_models" / "m").mkdir(parents=True)
    (baked / "fonts").mkdir()
    (baked / "fonts" / "simfang.ttf").write_bytes(b"font")
    env = {"AP_AGENT_PADDLEX_BAKED_DIR": str(baked), "PADDLE_PDX_CACHE_HOME": str(tmp_path / "work")}

    working = prepare_paddlex_cache(env)

    assert working == tmp_path / "work"
    assert (working / "official_models").is_symlink() and (working / "fonts" / "simfang.ttf").read_bytes() == b"font"
    (working / "locks").mkdir()  # PaddleX's own scratch directories are created in the writable location
    assert prepare_paddlex_cache(env) == working  # idempotent on a warm container


def test_paddle_cache_refuses_incomplete_or_unwritable_configuration(tmp_path):
    from ap_agent.aws.paddle_cache import PaddleCacheError, prepare_paddlex_cache

    baked = tmp_path / "baked"
    (baked / "official_models").mkdir(parents=True)  # no fonts directory

    with pytest.raises(PaddleCacheError, match="fonts"):
        prepare_paddlex_cache({"AP_AGENT_PADDLEX_BAKED_DIR": str(baked), "PADDLE_PDX_CACHE_HOME": str(tmp_path / "w")})

    with pytest.raises(PaddleCacheError, match="writable"):
        prepare_paddlex_cache({"AP_AGENT_PADDLEX_BAKED_DIR": str(baked), "PADDLE_PDX_CACHE_HOME": str(baked)})

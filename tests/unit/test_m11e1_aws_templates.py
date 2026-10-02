"""M11E.1: the AWS templates satisfy the cost, exposure, secret and least-privilege rules."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("verify_aws_templates", REPO / "scripts" / "verify_aws_templates.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)

TEMPLATE = REPO / "deploy" / "aws" / "template.yaml"


def _problems(mutate=None) -> list[str]:
    document = copy.deepcopy(verifier.load_template(TEMPLATE))

    if mutate:
        mutate(document)

    return verifier.verify_application_template(document)


def _fn(document, name):
    return document["Resources"][name]["Properties"]


def test_checked_in_templates_are_valid():
    assert verifier.verify_all() == []


@pytest.mark.parametrize(
    "resource",
    [
        {"Type": "AWS::EC2::NatGateway", "Properties": {}},
        {"Type": "AWS::ElasticLoadBalancingV2::LoadBalancer", "Properties": {}},
        {"Type": "AWS::RDS::DBInstance", "Properties": {}},
        {"Type": "AWS::OpenSearchService::Domain", "Properties": {}},
        {"Type": "AWS::ElastiCache::CacheCluster", "Properties": {}},
        {"Type": "AWS::Route53::HostedZone", "Properties": {}},
        {"Type": "AWS::ECS::Service", "Properties": {}},
        {"Type": "AWS::EC2::Instance", "Properties": {}},
    ],
)
def test_cost_forbidden_resources_are_rejected(resource):
    problems = _problems(lambda d: d["Resources"].update(Forbidden=resource))
    assert any("forbidden resource type" in problem for problem in problems)


def test_provisioned_concurrency_and_vpc_are_rejected():
    assert any("provisioned concurrency" in p for p in _problems(lambda d: _fn(d, "WebFunction").update(ProvisionedConcurrencyConfig={"ProvisionedConcurrentExecutions": 1})))
    assert any("provisioned concurrency" in p for p in _problems(lambda d: _fn(d, "WebFunction").update(AutoPublishAlias="live")))
    assert any("no VPC" in p for p in _problems(lambda d: _fn(d, "ApiFunction").update(VpcConfig={})))


def test_the_api_url_must_stay_iam_protected_and_only_the_frontend_public():
    assert any("AWS_IAM" in p for p in _problems(lambda d: _fn(d, "ApiFunction")["FunctionUrlConfig"].update(AuthType="NONE")))
    assert any("only AuthType NONE" in p for p in _problems(lambda d: _fn(d, "WebFunction")["FunctionUrlConfig"].update(AuthType="AWS_IAM")))
    assert any("must not have a Function URL" in p for p in _problems(lambda d: _fn(d, "MigrateFunction").update(FunctionUrlConfig={"AuthType": "NONE"})))
    assert any("must not have a Function URL" in p for p in _problems(lambda d: _fn(d, "WorkerFunction").update(FunctionUrlConfig={"AuthType": "NONE"})))


def test_worker_settings_are_pinned():
    assert any("Timeout" in p for p in _problems(lambda d: _fn(d, "WorkerFunction").update(Timeout=600)))
    assert any("BatchSize 1" in p for p in _problems(lambda d: _fn(d, "WorkerFunction")["Events"]["Jobs"]["Properties"].update(BatchSize=5)))
    assert any("reserved concurrency" in p for p in _problems(lambda d: d["Parameters"]["WorkerReservedConcurrency"].update(MaxValue=5)))
    assert any("4096" in p for p in _problems(lambda d: d["Parameters"]["WorkerMemoryMb"].update(Default=2048)))


def test_queue_rules():
    assert any("six times" in p for p in _problems(lambda d: d["Resources"]["JobsQueue"]["Properties"].update(VisibilityTimeout=900)))
    assert any("FIFO" in p for p in _problems(lambda d: d["Resources"]["JobsQueue"]["Properties"].update(FifoQueue=False)))
    assert any("redrive" in p for p in _problems(lambda d: d["Resources"]["JobsQueue"]["Properties"].pop("RedrivePolicy")))


def test_bucket_rules():
    assert any("Block Public Access" in p for p in _problems(lambda d: d["Resources"]["UploadBucket"]["Properties"]["PublicAccessBlockConfiguration"].update(BlockPublicPolicy=False)))
    assert any("BucketOwnerEnforced" in p for p in _problems(lambda d: d["Resources"]["UploadBucket"]["Properties"].pop("OwnershipControls")))
    assert any("encryption" in p for p in _problems(lambda d: d["Resources"]["UploadBucket"]["Properties"].pop("BucketEncryption")))
    assert any("expire automatically" in p for p in _problems(lambda d: d["Resources"]["UploadBucket"]["Properties"].pop("LifecycleConfiguration")))
    assert any("ACLs" in p for p in _problems(lambda d: d["Resources"]["UploadBucket"]["Properties"].update(AccessControl="PublicRead")))


def test_cors_cannot_widen():
    def widen(document):
        rule = document["Resources"]["UploadBucket"]["Properties"]["CorsConfiguration"]["Fn::If"][1]["CorsRules"][0]
        rule["AllowedMethods"] = ["POST", "GET"]

    assert any("CORS" in p for p in _problems(widen))


def test_secrets_cannot_enter_the_template():
    def secret_parameter(document):
        document["Parameters"]["PostgresDsn"] = {"Type": "String"}

    def secret_env(document):
        _fn(document, "ApiFunction")["Environment"]["Variables"]["AP_AGENT_POSTGRES_DSN"] = "x"

    def secret_value(document):
        _fn(document, "ApiFunction")["Environment"]["Variables"]["SOMETHING"] = "postgresql://u:p@h/db"

    assert any("looks like a secret" in p for p in _problems(secret_parameter))
    assert any("would hold a secret" in p for p in _problems(secret_env))
    assert any("secret-looking" in p for p in _problems(secret_value))
    assert any("secret-looking" in p for p in verifier.verify_application_template(verifier.load_template(TEMPLATE), raw_text="acct 123456789012"))


def test_least_privilege_boundaries():
    def widen_worker(document):
        statements = document["Resources"]["WorkerRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        statements.append({"Sid": "Oops", "Effect": "Allow", "Action": ["s3:PutObject"], "Resource": "arn:x"})

    def wildcard(document):
        document["Resources"]["ApiRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"].append(
            {"Sid": "Star", "Effect": "Allow", "Action": "s3:*", "Resource": "arn:x"}
        )

    def migration_dsn_to_api(document):
        document["Resources"]["ApiRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"].append(
            {"Sid": "Dsn", "Effect": "Allow", "Action": "ssm:GetParameter", "Resource": "parameter/postgres-migration-dsn"}
        )

    def worker_dispatches(document):
        document["Resources"]["WorkerRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"].append(
            {"Sid": "Send", "Effect": "Allow", "Action": "sqs:SendMessage", "Resource": "arn:x"}
        )

    def api_invokes_api(document):
        document["Resources"]["ApiRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"].append(
            {"Sid": "Self", "Effect": "Allow", "Action": "lambda:InvokeFunctionUrl", "Resource": "arn:x"}
        )

    assert any("read-only" in p for p in _problems(widen_worker))
    assert any("wildcard" in p for p in _problems(wildcard))
    assert any("migration/owner DSN" in p for p in _problems(migration_dsn_to_api))
    assert any("only the API dispatches" in p for p in _problems(worker_dispatches))
    assert any("only the frontend role" in p for p in _problems(api_invokes_api))


def test_logs_tags_outputs_and_alarms_are_required():
    assert any("retention" in p for p in _problems(lambda d: d["Resources"]["WebLogGroup"]["Properties"].pop("RetentionInDays")))
    assert any("Globals.Function.Tags" in p for p in _problems(lambda d: d["Globals"]["Function"].pop("Tags")))
    assert any("missing required tags" in p for p in _problems(lambda d: d["Resources"]["UploadBucket"]["Properties"].pop("Tags")))
    assert any("FrontendUrl" in p for p in _problems(lambda d: d["Outputs"].pop("FrontendUrl")))
    assert any("MigrationFunctionArn" in p for p in _problems(lambda d: d["Outputs"].pop("MigrationFunctionArn")))
    assert any("alarms" in p for p in _problems(lambda d: [d["Resources"].pop(n) for n in [k for k, v in d["Resources"].items() if v["Type"] == "AWS::CloudWatch::Alarm"]]))


def test_image_uris_come_from_parameters():
    assert any("ImageUri" in p for p in _problems(lambda d: _fn(d, "WebFunction").update(ImageUri="123.dkr.ecr/x:1")))

"""M11E.1/M11E.2: the AWS templates satisfy the cost, exposure, secret, Fargate and least-privilege rules."""

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
        {"Type": "AWS::EC2::EIP", "Properties": {}},
        {"Type": "AWS::EC2::VPCEndpoint", "Properties": {}},
        {"Type": "AWS::ECS::CapacityProvider", "Properties": {}},
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
    assert any("must not have a Function URL" in p for p in _problems(lambda d: _fn(d, "DispatcherFunction").update(FunctionUrlConfig={"AuthType": "NONE"})))


def test_dispatcher_settings_are_pinned():
    assert any("Timeout" in p for p in _problems(lambda d: _fn(d, "DispatcherFunction").update(Timeout=600)))
    assert any("BatchSize 1" in p for p in _problems(lambda d: _fn(d, "DispatcherFunction")["Events"]["Jobs"]["Properties"].update(BatchSize=5)))
    assert any("BatchSize 1" in p for p in _problems(lambda d: _fn(d, "DispatcherFunction")["Events"]["Jobs"]["Properties"].update(MaximumBatchingWindowInSeconds=30)))
    assert any("reserved concurrency" in p for p in _problems(lambda d: _fn(d, "DispatcherFunction").update(ReservedConcurrentExecutions=1)))
    assert any("reserved concurrency" in p for p in _problems(lambda d: d["Parameters"].update(WorkerReservedConcurrency={"Type": "Number"})))
    assert any("disabled by default" in p for p in _problems(lambda d: d["Parameters"]["WebAndApiReservedConcurrency"].update(Default=10)))
    assert any("heavy worker image" in p for p in _problems(lambda d: _fn(d, "DispatcherFunction").update(ImageUri={"Ref": "WorkerImageUri"})))
    assert any("ImageConfig.Command" in p for p in _problems(lambda d: _fn(d, "DispatcherFunction").update(ImageConfig={"Command": ["x.handler"]})))


def test_no_lambda_may_exceed_the_account_memory_cap():
    assert verifier.lambda_memory_problems(verifier.load_template(TEMPLATE)) == []
    # The exact regression of the failed real deployment: an 8 GB Lambda worker.
    assert any("MemorySize" in p for p in _problems(lambda d: _fn(d, "DispatcherFunction").update(MemorySize=8192)))
    assert any("MemorySize" in p for p in _problems(lambda d: _fn(d, "ApiFunction").update(MemorySize=3009)))
    assert any("MemorySize" in p for p in _problems(lambda d: _fn(d, "ApiFunction").update(MemorySize={"Ref": "WorkerMemoryMb"})))
    assert any("exceeds" in p for p in verifier.lambda_memory_problems({
        "Resources": {"W": {"Type": "AWS::Serverless::Function", "Properties": {"MemorySize": {"Ref": "M"}}}},
        "Parameters": {"M": {"Default": 8192}},
    }))
    assert any("exceeds" in p for p in verifier.lambda_memory_problems({
        "Resources": {"W": {"Type": "AWS::Lambda::Function", "Properties": {}}}, "Globals": {"Function": {"MemorySize": 4096}},
    }))


def test_the_ocr_worker_is_not_a_lambda_any_more():
    document = verifier.load_template(TEMPLATE)

    assert "WorkerFunction" not in document["Resources"] and "WorkerMemoryMb" not in document["Parameters"]
    assert any("must not be a Lambda" in p for p in _problems(lambda d: d["Resources"].update(WorkerFunction=copy.deepcopy(d["Resources"]["MigrateFunction"]))))


def _task(document):
    return document["Resources"]["OcrTaskDefinition"]["Properties"]


def test_the_fargate_task_keeps_the_benchmarked_envelope_and_no_service():
    document = verifier.load_template(TEMPLATE)
    task, container = _task(document), _task(document)["ContainerDefinitions"][0]

    assert (task["Cpu"], task["Memory"], task["NetworkMode"]) == ("4096", "8192", "awsvpc") and task["RequiresCompatibilities"] == ["FARGATE"]
    assert container["EntryPoint"] == ["python", "-m"] and container["Command"] == ["ap_agent.worker.dispatch_task"]
    assert not [n for n, r in document["Resources"].items() if r["Type"] == "AWS::ECS::Service"]

    assert any("4 vCPU" in p for p in _problems(lambda d: _task(d).update(Memory="3072")))
    assert any("4 vCPU" in p for p in _problems(lambda d: _task(d).update(Cpu="1024")))
    assert any("Fargate awsvpc" in p for p in _problems(lambda d: _task(d).update(NetworkMode="bridge")))
    assert any("ENTRYPOINT" in p for p in _problems(lambda d: _task(d)["ContainerDefinitions"][0].pop("EntryPoint")))
    assert any("WorkerImageUri" in p for p in _problems(lambda d: _task(d)["ContainerDefinitions"][0].update(Image="123456:latest")))
    assert any("awslogs" in p for p in _problems(lambda d: _task(d)["ContainerDefinitions"][0].pop("LogConfiguration")))
    assert any("injected secrets" in p for p in _problems(lambda d: _task(d)["ContainerDefinitions"][0].update(Secrets=[{"Name": "X", "ValueFrom": "y"}])))
    assert any("would hold a secret" in p for p in _problems(lambda d: _task(d)["ContainerDefinitions"][0]["Environment"].append({"Name": "AP_AGENT_POSTGRES_DSN", "Value": "x"})))
    assert any("secret-looking" in p for p in _problems(lambda d: _task(d)["ContainerDefinitions"][0]["Environment"].append({"Name": "OTHER", "Value": "postgresql://u:p@h/d"})))


def test_the_task_has_a_dedicated_log_group_dispatcher_env_and_outputs():
    document = verifier.load_template(TEMPLATE)

    assert document["Resources"]["OcrLogGroup"]["Properties"]["RetentionInDays"]
    assert {"OCR_CLUSTER_ARN", "OCR_TASK_DEFINITION_ARN", "OCR_SUBNET_IDS", "OCR_SECURITY_GROUP_IDS", "JOBS_QUEUE_URL"} <= set(
        document["Resources"]["DispatcherFunction"]["Properties"]["Environment"]["Variables"]
    )
    assert any("OCR_SUBNET_IDS" in p for p in _problems(lambda d: _fn(d, "DispatcherFunction")["Environment"]["Variables"].pop("OCR_SUBNET_IDS")))
    assert any("OcrClusterArn" in p for p in _problems(lambda d: d["Outputs"].pop("OcrClusterArn")))
    assert any("DispatcherFunctionArn" in p for p in _problems(lambda d: d["Outputs"].pop("DispatcherFunctionArn")))


def test_the_task_network_is_outbound_only_without_nat():
    document = verifier.load_template(TEMPLATE)
    group = document["Resources"]["OcrSecurityGroup"]["Properties"]

    assert "SecurityGroupIngress" not in group
    assert {rule["FromPort"] for rule in group["SecurityGroupEgress"]} == {443, 5432}
    assert not [n for n, r in document["Resources"].items() if r["Type"] in ("AWS::EC2::NatGateway", "AWS::EC2::EIP", "AWS::EC2::VPCEndpoint")]

    def open_inbound(d):
        d["Resources"]["OcrSecurityGroup"]["Properties"]["SecurityGroupIngress"] = [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "CidrIp": "0.0.0.0/0"}]

    def open_egress(d):
        d["Resources"]["OcrSecurityGroup"]["Properties"]["SecurityGroupEgress"].append({"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"})

    assert any("no inbound" in p for p in _problems(open_inbound))
    assert any("outbound is limited" in p for p in _problems(open_egress))
    assert any("public IPs" in p for p in _problems(lambda d: d["Resources"]["OcrSubnetA"]["Properties"].update(MapPublicIpOnLaunch=True)))


def _statements(document, role):
    return document["Resources"][role]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]


def test_the_dispatcher_ecs_permissions_are_narrow():
    def widen_run(d):
        for statement in _statements(d, "DispatcherRole"):
            if statement["Sid"] == "RunOcrTask":
                statement.pop("Condition")

    def widen_pass_role(d):
        for statement in _statements(d, "DispatcherRole"):
            if statement["Sid"] == "PassTaskRoles":
                statement["Condition"] = {}

    def star_pass_role(d):
        for statement in _statements(d, "DispatcherRole"):
            if statement["Sid"] == "PassTaskRoles":
                statement["Resource"] = "*"

    def widen_stop(d):
        for statement in _statements(d, "DispatcherRole"):
            if statement["Sid"] == "ObserveAndStopOcrTasks":
                statement["Resource"] = "arn:aws:ecs:eu-west-2:x:task/other/*"

    assert any("RunTask must be limited" in p for p in _problems(widen_run))
    assert any("PassRole must be limited" in p for p in _problems(widen_pass_role))
    assert any("PassRole must be limited" in p or "wildcard resource" in p for p in _problems(star_pass_role))
    assert any("limited to tasks of the OCR cluster" in p for p in _problems(widen_stop))


def test_the_ocr_roles_are_separated():
    def execution_reads_secrets(d):
        _statements(d, "OcrExecutionRole").append({"Sid": "Oops", "Effect": "Allow", "Action": "ssm:GetParameter", "Resource": "arn:x"})

    def task_writes_invoices(d):
        _statements(d, "OcrTaskRole").append({"Sid": "Oops", "Effect": "Allow", "Action": ["s3:PutObject"], "Resource": "arn:x"})

    def task_reads_migration_dsn(d):
        _statements(d, "OcrTaskRole").append({"Sid": "Oops", "Effect": "Allow", "Action": "ssm:GetParameter", "Resource": "parameter/postgres-migration-dsn"})

    def task_starts_tasks(d):
        _statements(d, "OcrTaskRole").append({"Sid": "Oops", "Effect": "Allow", "Action": "ecs:RunTask", "Resource": "arn:x"})

    def dispatcher_reads_secrets(d):
        _statements(d, "DispatcherRole").append({"Sid": "Oops", "Effect": "Allow", "Action": "ssm:GetParameter", "Resource": "arn:x"})

    def dispatcher_touches_invoices(d):
        _statements(d, "DispatcherRole").append({"Sid": "Oops", "Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:x"})

    def execution_unscoped_pull(d):
        for statement in _statements(d, "OcrExecutionRole"):
            if statement["Sid"] == "PullWorkerImage":
                statement["Resource"] = "arn:aws:ecr:eu-west-2:x:repository/other"

    def unscoped_star(d):
        _statements(d, "OcrExecutionRole").append({"Sid": "Oops", "Effect": "Allow", "Action": "logs:PutLogEvents", "Resource": "*"})

    assert any("must not read any SSM" in p for p in _problems(execution_reads_secrets))
    assert any("read-only" in p for p in _problems(task_writes_invoices))
    assert any("migration/owner DSN" in p for p in _problems(task_reads_migration_dsn))
    assert any("must not start tasks" in p for p in _problems(task_starts_tasks))
    assert any("must not read any SSM" in p for p in _problems(dispatcher_reads_secrets))
    assert any("S3 access" in p for p in _problems(dispatcher_touches_invoices))
    assert any("limited to the worker repository" in p for p in _problems(execution_unscoped_pull))
    assert any("wildcard resource" in p for p in _problems(unscoped_star))


def test_the_image_parameters_demand_an_explicit_ecr_uri_and_the_task_uses_the_worker_one():
    import re

    document = verifier.load_template(TEMPLATE)
    pattern = re.compile(document["Parameters"]["WorkerImageUri"]["AllowedPattern"])

    assert pattern.match("123456789012.dkr.ecr.eu-west-2.amazonaws.com/ap-agent-production/worker:abc123def456")
    assert pattern.match("123456789012.dkr.ecr.eu-west-2.amazonaws.com/ap-agent-production/worker@sha256:" + "a" * 64)
    assert not pattern.match("worker:latest") and not pattern.match("docker.io/library/python:3.11")

    for name in ("WebImageUri", "ApiImageUri", "MigrateImageUri", "WorkerImageUri"):
        assert document["Parameters"][name]["AllowedPattern"] == document["Parameters"]["WorkerImageUri"]["AllowedPattern"]


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
        statements = document["Resources"]["OcrTaskRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
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
        document["Resources"]["OcrTaskRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"].append(
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
    assert any("retention" in p for p in _problems(lambda d: d["Resources"]["OcrLogGroup"]["Properties"].pop("RetentionInDays")))
    assert any("Globals.Function.Tags" in p for p in _problems(lambda d: d["Globals"]["Function"].pop("Tags")))
    assert any("missing required tags" in p for p in _problems(lambda d: d["Resources"]["UploadBucket"]["Properties"].pop("Tags")))
    assert any("FrontendUrl" in p for p in _problems(lambda d: d["Outputs"].pop("FrontendUrl")))
    assert any("MigrationFunctionArn" in p for p in _problems(lambda d: d["Outputs"].pop("MigrationFunctionArn")))
    assert any("alarms" in p for p in _problems(lambda d: [d["Resources"].pop(n) for n in [k for k, v in d["Resources"].items() if v["Type"] == "AWS::CloudWatch::Alarm"]]))


def test_image_uris_come_from_parameters():
    assert any("ImageUri" in p for p in _problems(lambda d: _fn(d, "WebFunction").update(ImageUri="123.dkr.ecr/x:1")))


def test_the_dispatcher_has_no_reserved_concurrency_and_stays_single_through_the_fifo_design():
    document = verifier.load_template(TEMPLATE)
    dispatcher = document["Resources"]["DispatcherFunction"]["Properties"]
    event = dispatcher["Events"]["Jobs"]["Properties"]

    assert "ReservedConcurrentExecutions" not in dispatcher and "WorkerReservedConcurrency" not in document["Parameters"]
    assert event["BatchSize"] == 1 and "MaximumBatchingWindowInSeconds" not in event
    assert "ScalingConfig" not in event  # no raised event-source concurrency
    assert document["Resources"]["JobsQueue"]["Properties"]["FifoQueue"] is True
    # The optional web/API cap is off unless asked for, and must stay a valid -1.
    web_api = document["Parameters"]["WebAndApiReservedConcurrency"]
    assert web_api["Default"] == -1 and web_api["MinValue"] == -1
    assert "ReserveWebAndApiConcurrency" in document["Conditions"]
    for name in ("ApiFunction", "WebFunction"):
        assert "If" in str(document["Resources"][name]["Properties"]["ReservedConcurrentExecutions"])


def test_the_obsolete_fargate_fallback_template_is_gone():
    assert not (REPO / "deploy" / "aws" / "fargate-fallback.yaml").exists()

#!/usr/bin/env python3
"""Static verification of the AWS deployment templates (`deploy/aws/*.yaml`) against the
M11E.1 rules. Needs PyYAML only (no AWS access, no CloudFormation service). Exits non-zero
and lists every violation. Complements `cfn-lint` / `sam validate --lint`, which check
syntax and schema; this checks the *policy*: cost controls, public/private surfaces,
secret handling and least privilege.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
AWS_DIR = REPO / "deploy" / "aws"

REQUIRED_OUTPUTS = (
    "FrontendUrl", "ApiUrl", "UploadBucketName", "QueueUrl", "WorkerFunctionArn", "MigrationFunctionArn",
)
REQUIRED_TAGS = ("Project", "Environment", "ManagedBy")
FORBIDDEN_RESOURCE_PREFIXES = (
    "AWS::EC2::NatGateway", "AWS::EC2::Instance", "AWS::EC2::VPC", "AWS::EC2::Subnet", "AWS::EC2::EIP",
    "AWS::ElasticLoadBalancing", "AWS::ElasticLoadBalancingV2", "AWS::RDS::", "AWS::OpenSearchService::",
    "AWS::Elasticsearch::", "AWS::ElastiCache::", "AWS::Route53", "AWS::ECS::Service", "AWS::AutoScaling",
    "AWS::ApplicationAutoScaling", "AWS::CloudFront::", "AWS::Amplify", "AWS::ApiGateway", "AWS::ApiGatewayV2",
    "AWS::Lambda::Alias", "AWS::Lambda::Version", "AWS::EKS::", "AWS::DynamoDB::",
)
SECRET_VALUE = re.compile(
    r"(postgres(ql)?://|sk_(live|test)_[A-Za-z0-9]|pk_(live|test)_[A-Za-z0-9]{8}|-----BEGIN|AKIA[0-9A-Z]{8,}|ASIA[0-9A-Z]{8,}"
    r"|\b\d{12}\b|X-Amz-Security-Token)"
)
SECRET_PARAMETER_NAME = re.compile(r"(dsn|secret|password|token|private|access ?key)", re.I)


class _Loader(yaml.SafeLoader):
    """CloudFormation short-form tags (`!Ref`, `!Sub`, ...) become `{"Fn::X": value}` / `{"Ref": value}`."""


def _construct(loader: yaml.Loader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)

    if suffix == "Ref":
        return {"Ref": value}

    if suffix == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)

    return {f"Fn::{suffix}": value}


_Loader.add_multi_constructor("!", _construct)


def load_template(path: Path) -> dict[str, Any]:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506 - restricted loader


def _resources(template: dict[str, Any], resource_type: str) -> dict[str, dict[str, Any]]:
    return {name: body for name, body in template["Resources"].items() if body.get("Type") == resource_type}


def _statements(role: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for policy in role["Properties"]["Policies"] for s in policy["PolicyDocument"]["Statement"]]


def _mentions(value: Any, needle: str) -> bool:
    return needle in json.dumps(value)


def verify_application_template(template: dict[str, Any], *, raw_text: str = "") -> list[str]:
    problems: list[str] = []
    resources = template["Resources"]
    parameters = template.get("Parameters", {})

    # -- cost controls ----------------------------------------------------------------------
    for name, body in resources.items():
        kind = body.get("Type", "")

        if any(kind.startswith(prefix) for prefix in FORBIDDEN_RESOURCE_PREFIXES):
            problems.append(f"{name}: forbidden resource type {kind} (cost/scope rule)")

        props = body.get("Properties", {})

        if "ProvisionedConcurrencyConfig" in props or "AutoPublishAlias" in props:
            problems.append(f"{name}: provisioned concurrency is forbidden")

        if "VpcConfig" in props:
            problems.append(f"{name}: no VPC (no NAT gateway, scale to zero)")

    # -- functions --------------------------------------------------------------------------
    functions = _resources(template, "AWS::Serverless::Function")
    expected = {"ApiFunction", "WebFunction", "WorkerFunction", "MigrateFunction"}

    if set(functions) != expected:
        problems.append(f"expected exactly the functions {sorted(expected)}, found {sorted(functions)}")

    for name, body in functions.items():
        props = body["Properties"]

        if props.get("PackageType") != "Image":
            problems.append(f"{name}: must be a container image")

        if not (isinstance(props.get("ImageUri"), dict) and "Ref" in props["ImageUri"]):
            problems.append(f"{name}: ImageUri must come from a parameter")

    api, web, worker, migrate = (functions.get(n, {}).get("Properties", {}) for n in ("ApiFunction", "WebFunction", "WorkerFunction", "MigrateFunction"))

    if api.get("FunctionUrlConfig", {}).get("AuthType") != "AWS_IAM":
        problems.append("ApiFunction: Function URL must use AuthType AWS_IAM")

    if web.get("FunctionUrlConfig", {}).get("AuthType") != "NONE":
        problems.append("WebFunction: the public frontend URL must be the only AuthType NONE URL")

    for name, props in (("WorkerFunction", worker), ("MigrateFunction", migrate)):
        if "FunctionUrlConfig" in props:
            problems.append(f"{name}: must not have a Function URL")

    if migrate.get("Events"):
        problems.append("MigrateFunction: must not have an event source (explicit invocation only)")

    if worker.get("Timeout") != 900:
        problems.append("WorkerFunction: Timeout must be 900")

    if worker.get("ReservedConcurrentExecutions") != {"Ref": "WorkerReservedConcurrency"} or (
        parameters.get("WorkerReservedConcurrency", {}).get("Default") != 1
        or parameters["WorkerReservedConcurrency"].get("MaxValue") != 1
    ):
        problems.append("WorkerFunction: reserved concurrency must be exactly 1")

    if parameters.get("WorkerMemoryMb", {}).get("Default", 0) < 4096:
        problems.append("WorkerFunction: memory must default to at least 4096 MB")

    if worker.get("EphemeralStorage", {}).get("Size") is None:
        problems.append("WorkerFunction: ephemeral storage must be configured")

    event = next(iter(worker.get("Events", {}).values()), {})
    sqs = event.get("Properties", {})

    if event.get("Type") != "SQS" or sqs.get("BatchSize") != 1 or "ReportBatchItemFailures" not in sqs.get("FunctionResponseTypes", []):
        problems.append("WorkerFunction: must be triggered by SQS with BatchSize 1 and ReportBatchItemFailures")

    # -- queue ------------------------------------------------------------------------------
    queues = _resources(template, "AWS::SQS::Queue")
    main = queues.get("JobsQueue", {}).get("Properties", {})
    dead = queues.get("JobsDeadLetterQueue", {}).get("Properties", {})

    if not main.get("FifoQueue") or not dead.get("FifoQueue"):
        problems.append("queues: both the main queue and the dead-letter queue must be FIFO")

    if main.get("VisibilityTimeout", 0) < 6 * worker.get("Timeout", 900):
        problems.append("JobsQueue: VisibilityTimeout must be at least six times the worker timeout")

    redrive = main.get("RedrivePolicy", {})

    if not _mentions(redrive.get("deadLetterTargetArn"), "JobsDeadLetterQueue") or not redrive.get("maxReceiveCount"):
        problems.append("JobsQueue: a redrive policy to the dead-letter queue is required")

    # -- bucket -----------------------------------------------------------------------------
    bucket = resources.get("UploadBucket", {}).get("Properties", {})
    block = bucket.get("PublicAccessBlockConfiguration", {})

    if not all(block.get(key) is True for key in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets")):
        problems.append("UploadBucket: Block Public Access must be fully enabled")

    if "AccessControl" in bucket:
        problems.append("UploadBucket: ACLs are not used")

    if bucket.get("OwnershipControls", {}).get("Rules", [{}])[0].get("ObjectOwnership") != "BucketOwnerEnforced":
        problems.append("UploadBucket: ObjectOwnership must be BucketOwnerEnforced")

    if not bucket.get("BucketEncryption"):
        problems.append("UploadBucket: encryption at rest is required")

    rules = bucket.get("LifecycleConfiguration", {}).get("Rules", [])

    if not any(rule.get("Prefix") == "staging/" and rule.get("ExpirationInDays") for rule in rules):
        problems.append("UploadBucket: staged uploads must expire automatically")

    cors = json.dumps(bucket.get("CorsConfiguration", {}))

    if '"*"' in cors.replace('"Ref": "FrontendOrigin"', "") or "AllowedMethods" in cors and any(m in cors for m in ('"GET"', '"PUT"', '"DELETE"', '"HEAD"')):
        problems.append("UploadBucket: CORS must be limited to POST from the frontend origin")

    # -- logs, tags, outputs ------------------------------------------------------------------
    log_groups = _resources(template, "AWS::Logs::LogGroup")

    if len(log_groups) != 4 or not all(group["Properties"].get("RetentionInDays") for group in log_groups.values()):
        problems.append("every function needs a log group with a retention period")

    function_tags = template.get("Globals", {}).get("Function", {}).get("Tags", {})

    if not all(tag in function_tags for tag in REQUIRED_TAGS) or function_tags.get("Project") != "accounts-payable-agent" or function_tags.get("ManagedBy") != "cloudformation":
        problems.append("Globals.Function.Tags must set Project, Environment and ManagedBy")

    for kind in ("AWS::S3::Bucket", "AWS::SQS::Queue", "AWS::IAM::Role", "AWS::SNS::Topic"):
        for name, body in _resources(template, kind).items():
            keys = {tag["Key"] for tag in body["Properties"].get("Tags", [])}

            if not set(REQUIRED_TAGS) <= keys:
                problems.append(f"{name}: missing required tags")

    if parameters.get("Environment", {}).get("Default") != "production":
        problems.append("Environment parameter must default to production")

    for output in REQUIRED_OUTPUTS:
        if output not in template.get("Outputs", {}):
            problems.append(f"missing required output {output}")

    if not _resources(template, "AWS::CloudWatch::Alarm"):
        problems.append("alarms are required")

    # -- secrets ------------------------------------------------------------------------------
    for name in parameters:
        if SECRET_PARAMETER_NAME.search(name):
            problems.append(f"parameter {name} looks like a secret: secrets live in SSM SecureString, never in the template")

    for name, body in functions.items():
        variables = body["Properties"].get("Environment", {}).get("Variables", {})

        for key, value in variables.items():
            if re.search(r"(SECRET|PASSWORD|_DSN|ACCESS_KEY|PRIVATE|JWT_KEY)", key) and key != "AP_AGENT_SSM_PARAMETERS":
                problems.append(f"{name}: environment variable {key} would hold a secret value (use AP_AGENT_SSM_PARAMETERS)")

            if isinstance(value, str) and SECRET_VALUE.search(value):
                problems.append(f"{name}: environment variable {key} contains secret-looking data")

    if raw_text and SECRET_VALUE.search(raw_text):
        problems.append("the template text contains secret-looking data (connection string, key or account id)")

    # -- least privilege ----------------------------------------------------------------------
    roles = _resources(template, "AWS::IAM::Role")

    for name, role in roles.items():
        for statement in _statements(role):
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]

            if any(action == "*" or action.endswith(":*") for action in actions) or statement.get("Resource") == "*":
                problems.append(f"{name}: wildcard action or resource in statement {statement.get('Sid')}")

    def role_text(name: str) -> str:
        return json.dumps(roles[name]["Properties"]["Policies"])

    if "postgres-migration-dsn" in role_text("WebRole") + role_text("ApiRole") + role_text("WorkerRole"):
        problems.append("only the migration role may read the migration/owner DSN")

    if "postgres-migration-dsn" not in role_text("MigrateRole") or "postgres-runtime-dsn" in role_text("MigrateRole"):
        problems.append("MigrateRole: must read the migration DSN and nothing else secret")

    for name in ("WebRole", "WorkerRole", "MigrateRole"):
        if "sqs:SendMessage" in role_text(name):
            problems.append(f"{name}: only the API dispatches jobs")

    if "s3:PutObject" in role_text("WorkerRole") or "s3:DeleteObject" in role_text("WorkerRole") or "s3:" in role_text("WebRole") or "s3:" in role_text("MigrateRole"):
        problems.append("S3 access: the worker is read-only; the web and migration roles have none")

    invoke = [s for s in _statements(roles["WebRole"]) if "lambda:InvokeFunctionUrl" in json.dumps(s["Action"])]

    if len(invoke) != 1 or not _mentions(invoke[0]["Resource"], "ApiFunction"):
        problems.append("WebRole: must be allowed to invoke exactly the API Function URL")

    for name in ("ApiRole", "WorkerRole", "MigrateRole"):
        if "lambda:InvokeFunctionUrl" in role_text(name):
            problems.append(f"{name}: only the frontend role may invoke the API")

    return problems


def verify_ecr_template(template: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    repositories = _resources(template, "AWS::ECR::Repository")

    if len(repositories) != 4:
        problems.append("expected four image repositories (web, api, worker, migrate)")

    for name, body in repositories.items():
        props = body["Properties"]

        if not props.get("LifecyclePolicy"):
            problems.append(f"{name}: an image lifecycle policy is required")

        keys = {tag["Key"] for tag in props.get("Tags", [])}

        if not set(REQUIRED_TAGS) <= keys:
            problems.append(f"{name}: missing required tags")

    return problems


def verify_all(directory: Path = AWS_DIR) -> list[str]:
    application = directory / "template.yaml"
    problems = verify_application_template(load_template(application), raw_text=application.read_text(encoding="utf-8"))
    problems += [f"ecr.yaml: {p}" for p in verify_ecr_template(load_template(directory / "ecr.yaml"))]

    for name in ("template.yaml", "ecr.yaml", "fargate-fallback.yaml"):
        text = (directory / name).read_text(encoding="utf-8")

        if SECRET_VALUE.search(text):
            problems.append(f"{name}: contains secret-looking data")

    fallback = load_template(directory / "fargate-fallback.yaml")

    if _resources(fallback, "AWS::ECS::Service") or _resources(fallback, "AWS::EC2::NatGateway") or _resources(fallback, "AWS::ElasticLoadBalancingV2::LoadBalancer"):
        problems.append("fargate-fallback.yaml: no always-on service, NAT gateway or load balancer")

    return problems


def main() -> int:
    problems = verify_all()

    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)

    print("AWS templates OK." if not problems else f"{len(problems)} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

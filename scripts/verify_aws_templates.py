#!/usr/bin/env python3
"""Static verification of the AWS deployment templates (`deploy/aws/*.yaml`) against the
M11E.1 / M11E.2 rules (heavy OCR runs on on-demand ECS Fargate, never in a >3,008 MB Lambda). Needs PyYAML only (no AWS access, no CloudFormation service). Exits non-zero
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

LAMBDA_ACCOUNT_MEMORY_LIMIT_MB = 3008  # this AWS account's observed cap (the real deployment failed above it)
WORKER_MINIMUM_MEMORY_MB = 8192
REQUIRED_OUTPUTS = (
    "FrontendUrl", "ApiUrl", "UploadBucketName", "QueueUrl", "DispatcherFunctionArn", "MigrationFunctionArn",
    "OcrClusterArn", "OcrTaskDefinitionArn", "OcrSubnetIds", "OcrSecurityGroupId",
)
REQUIRED_TAGS = ("Project", "Environment", "ManagedBy")
FORBIDDEN_RESOURCE_PREFIXES = (
    "AWS::EC2::NatGateway", "AWS::EC2::Instance", "AWS::EC2::EIP", "AWS::EC2::VPCEndpoint", "AWS::EC2::TransitGateway",
    "AWS::EC2::VPNConnection", "AWS::EC2::LaunchTemplate", "AWS::ECS::CapacityProvider",
    "AWS::ElasticLoadBalancing", "AWS::ElasticLoadBalancingV2", "AWS::RDS::", "AWS::OpenSearchService::",
    "AWS::Elasticsearch::", "AWS::ElastiCache::", "AWS::Route53", "AWS::ECS::Service", "AWS::ECS::TaskSet", "AWS::AutoScaling",
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
    expected = {"ApiFunction", "WebFunction", "DispatcherFunction", "MigrateFunction"}

    if set(functions) != expected:
        problems.append(f"expected exactly the functions {sorted(expected)}, found {sorted(functions)}")

    for name, body in functions.items():
        props = body["Properties"]

        if props.get("PackageType") != "Image":
            problems.append(f"{name}: must be a container image")

        if not (isinstance(props.get("ImageUri"), dict) and "Ref" in props["ImageUri"]):
            problems.append(f"{name}: ImageUri must come from a parameter")

        memory = props.get("MemorySize")

        if not isinstance(memory, int) or memory > LAMBDA_ACCOUNT_MEMORY_LIMIT_MB:
            problems.append(f"{name}: Lambda MemorySize must be a literal at most {LAMBDA_ACCOUNT_MEMORY_LIMIT_MB} MB (the account cap)")

    api, web, dispatcher, migrate = (functions.get(n, {}).get("Properties", {}) for n in ("ApiFunction", "WebFunction", "DispatcherFunction", "MigrateFunction"))

    if api.get("FunctionUrlConfig", {}).get("AuthType") != "AWS_IAM":
        problems.append("ApiFunction: Function URL must use AuthType AWS_IAM")

    if web.get("FunctionUrlConfig", {}).get("AuthType") != "NONE":
        problems.append("WebFunction: the public frontend URL must be the only AuthType NONE URL")

    for name, props in (("DispatcherFunction", dispatcher), ("MigrateFunction", migrate)):
        if "FunctionUrlConfig" in props:
            problems.append(f"{name}: must not have a Function URL")

    if migrate.get("Events"):
        problems.append("MigrateFunction: must not have an event source (explicit invocation only)")

    if dispatcher.get("Timeout") != 900:
        problems.append("DispatcherFunction: Timeout must be 900")

    if _mentions(dispatcher.get("ImageUri"), "WorkerImageUri"):
        problems.append("DispatcherFunction: must not use the heavy worker image")

    if dispatcher.get("ImageConfig", {}).get("Command") != ["ap_agent.aws.fargate_dispatcher.handler"]:
        problems.append("DispatcherFunction: ImageConfig.Command must be ap_agent.aws.fargate_dispatcher.handler")

    # No reservation on the dispatcher: it would need an account quota of at least 101. Single active task comes from the
    # FIFO design (one message group, batch size 1), checked below and in test_m11e1_queue_worker.py.
    if "ReservedConcurrentExecutions" in dispatcher or "WorkerReservedConcurrency" in parameters:
        problems.append("DispatcherFunction: must not use reserved concurrency (needs a quota of at least 101)")

    if "WorkerFunction" in resources or "WorkerMemoryMb" in parameters:
        problems.append("the OCR worker must not be a Lambda function (account memory cap): it runs on Fargate")

    web_api = parameters.get("WebAndApiReservedConcurrency", {})

    if web_api.get("Default") != -1 or web_api.get("MinValue") != -1:
        problems.append("WebAndApiReservedConcurrency: optional and disabled by default (-1)")

    event = next(iter(dispatcher.get("Events", {}).values()), {})
    sqs = event.get("Properties", {})

    if event.get("Type") != "SQS" or sqs.get("BatchSize") != 1 or "MaximumBatchingWindowInSeconds" in sqs or "ScalingConfig" in sqs:
        problems.append("DispatcherFunction: must be triggered by SQS with BatchSize 1 and no batching window or scaling config")

    problems += _verify_fargate(template)

    # -- queue ------------------------------------------------------------------------------
    queues = _resources(template, "AWS::SQS::Queue")
    main = queues.get("JobsQueue", {}).get("Properties", {})
    dead = queues.get("JobsDeadLetterQueue", {}).get("Properties", {})

    if not main.get("FifoQueue") or not dead.get("FifoQueue"):
        problems.append("queues: both the main queue and the dead-letter queue must be FIFO")

    if main.get("VisibilityTimeout", 0) < 6 * dispatcher.get("Timeout", 900):
        problems.append("JobsQueue: VisibilityTimeout must be at least six times the dispatcher timeout")

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

    if len(log_groups) != 5 or not all(group["Properties"].get("RetentionInDays") for group in log_groups.values()):
        problems.append("every function and the OCR task need a log group with a retention period")

    function_tags = template.get("Globals", {}).get("Function", {}).get("Tags", {})

    if not all(tag in function_tags for tag in REQUIRED_TAGS) or function_tags.get("Project") != "accounts-payable-agent" or function_tags.get("ManagedBy") != "cloudformation":
        problems.append("Globals.Function.Tags must set Project, Environment and ManagedBy")

    for kind in ("AWS::S3::Bucket", "AWS::SQS::Queue", "AWS::IAM::Role", "AWS::SNS::Topic", "AWS::ECS::Cluster", "AWS::ECS::TaskDefinition", "AWS::EC2::VPC", "AWS::EC2::SecurityGroup"):
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

            if any(action == "*" or action.endswith(":*") for action in actions):
                problems.append(f"{name}: wildcard action in statement {statement.get('Sid')}")

            # ecr:GetAuthorizationToken is the one action AWS does not let you scope to a resource.
            if statement.get("Resource") == "*" and actions != ["ecr:GetAuthorizationToken"]:
                problems.append(f"{name}: wildcard resource in statement {statement.get('Sid')}")

    def role_text(name: str) -> str:
        return json.dumps(roles[name]["Properties"]["Policies"])

    runtime_roles = ("WebRole", "ApiRole", "DispatcherRole", "OcrExecutionRole", "OcrTaskRole")

    if any(name not in roles for name in runtime_roles + ("MigrateRole",)):
        return problems + ["expected the roles " + ", ".join(runtime_roles + ("MigrateRole",))]

    if "postgres-migration-dsn" in "".join(role_text(name) for name in runtime_roles):
        problems.append("only the migration role may read the migration/owner DSN")

    if "postgres-migration-dsn" not in role_text("MigrateRole") or "postgres-runtime-dsn" in role_text("MigrateRole"):
        problems.append("MigrateRole: must read the migration DSN and nothing else secret")

    for name in ("WebRole", "DispatcherRole", "OcrExecutionRole", "OcrTaskRole", "MigrateRole"):
        if "sqs:SendMessage" in role_text(name):
            problems.append(f"{name}: only the API dispatches jobs")

    for name in ("WebRole", "DispatcherRole", "OcrExecutionRole", "MigrateRole"):
        if "s3:" in role_text(name):
            problems.append(f"S3 access: {name} has none (the API writes, the OCR task role only reads)")

    if "s3:PutObject" in role_text("OcrTaskRole") or "s3:DeleteObject" in role_text("OcrTaskRole"):
        problems.append("S3 access: the OCR task role is read-only")

    # Only the OCR task role (never the execution role, the dispatcher or the web role) may read the runtime DSN besides the API.
    for name in ("DispatcherRole", "OcrExecutionRole", "WebRole"):
        if "ssm:" in role_text(name) and name != "WebRole":
            problems.append(f"{name}: must not read any SSM parameter (secrets are read by the OCR process via its task role)")

    if "postgres-runtime-dsn" not in role_text("OcrTaskRole"):
        problems.append("OcrTaskRole: must read the runtime DSN (and nothing else secret)")

    for name in ("DispatcherRole", "OcrExecutionRole", "OcrTaskRole"):
        for statement in _statements(roles[name]):
            if "sqs:" in json.dumps(statement["Action"]) and name != "DispatcherRole":
                problems.append(f"{name}: no queue access (the dispatcher consumes and acknowledges messages)")

    for name in ("WebRole", "OcrExecutionRole", "OcrTaskRole", "MigrateRole"):
        if "ecs:" in role_text(name) or "iam:PassRole" in role_text(name):
            problems.append(f"{name}: must not start tasks or pass roles (only the dispatcher may)")

    invoke = [s for s in _statements(roles["WebRole"]) if "lambda:InvokeFunctionUrl" in json.dumps(s["Action"])]

    if len(invoke) != 1 or not _mentions(invoke[0]["Resource"], "ApiFunction"):
        problems.append("WebRole: must be allowed to invoke exactly the API Function URL")

    # Function URLs created since October 2025 need BOTH lambda:InvokeFunctionUrl and lambda:InvokeFunction. Each is a separate
    # statement on exactly the API function, and each is restricted (auth type AWS_IAM / invoked via a Function URL).
    via_url = [s for s in _statements(roles["WebRole"]) if "lambda:InvokeFunction" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]

    if len(via_url) != 1 or not _mentions(via_url[0]["Resource"], "ApiFunction") or via_url[0].get("Effect") != "Allow":
        problems.append("WebRole: must hold exactly one lambda:InvokeFunction statement, on the API function only")
    elif via_url[0].get("Condition") != {"Bool": {"lambda:InvokedViaFunctionUrl": "true"}}:
        problems.append("WebRole: lambda:InvokeFunction must be restricted to Function URL invocations (lambda:InvokedViaFunctionUrl true)")
    elif isinstance(via_url[0]["Resource"], list) or via_url[0]["Resource"] != {"Fn::GetAtt": ["ApiFunction", "Arn"]}:
        problems.append("WebRole: lambda:InvokeFunction must name only the API function ARN")

    if len(invoke) == 1 and invoke[0].get("Condition") != {"StringEquals": {"lambda:FunctionUrlAuthType": "AWS_IAM"}}:
        problems.append("WebRole: lambda:InvokeFunctionUrl must stay restricted to FunctionUrlAuthType AWS_IAM")

    for name in ("ApiRole", "DispatcherRole", "OcrExecutionRole", "OcrTaskRole", "MigrateRole"):
        if "lambda:InvokeFunction" in role_text(name):
            problems.append(f"{name}: only the frontend role may invoke the API")

    for name in ("ApiRole", "DispatcherRole", "OcrExecutionRole", "OcrTaskRole", "MigrateRole"):
        if "lambda:InvokeFunctionUrl" in role_text(name):
            problems.append(f"{name}: only the frontend role may invoke the API")

    return problems


def _verify_fargate(template: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    resources = template["Resources"]
    definitions = _resources(template, "AWS::ECS::TaskDefinition")
    definition = definitions.get("OcrTaskDefinition", {}).get("Properties")

    if len(definitions) != 1 or definition is None:
        return ["expected exactly one ECS task definition named OcrTaskDefinition"]

    if "FARGATE" not in definition.get("RequiresCompatibilities", []) or definition.get("NetworkMode") != "awsvpc":
        problems.append("OcrTaskDefinition: must be a Fargate awsvpc task")

    if str(definition.get("Cpu")) != "4096" or str(definition.get("Memory")) != str(WORKER_MINIMUM_MEMORY_MB):
        problems.append("OcrTaskDefinition: must keep the benchmarked 4 vCPU (4096) / 8192 MB")

    for role in ("ExecutionRoleArn", "TaskRoleArn"):
        if not _mentions(definition.get(role), "Ocr"):
            problems.append(f"OcrTaskDefinition: {role} must be the dedicated OCR role")

    containers = definition.get("ContainerDefinitions", [])

    if len(containers) != 1 or containers[0].get("Name") != "worker" or containers[0].get("Essential") is not True:
        problems.append("OcrTaskDefinition: exactly one essential container named worker")
        return problems

    container = containers[0]

    if container.get("Image") != {"Ref": "WorkerImageUri"}:
        problems.append("OcrTaskDefinition: the image must come from the WorkerImageUri parameter")

    if container.get("EntryPoint") != ["python", "-m"] or container.get("Command") != ["ap_agent.worker.dispatch_task"]:
        problems.append("OcrTaskDefinition: must override the Lambda ENTRYPOINT and run python -m ap_agent.worker.dispatch_task")

    if container.get("LogConfiguration", {}).get("LogDriver") != "awslogs":
        problems.append("OcrTaskDefinition: container logs must go to CloudWatch (awslogs)")

    if "Secrets" in container:
        problems.append("OcrTaskDefinition: no ECS-injected secrets (the process reads SSM through its task role)")

    for variable in container.get("Environment", []):
        name, value = variable.get("Name", ""), variable.get("Value")

        if re.search(r"(SECRET|PASSWORD|_DSN|ACCESS_KEY|PRIVATE|JWT_KEY)", name):
            problems.append(f"OcrTaskDefinition: environment variable {name} would hold a secret value")

        if isinstance(value, str) and SECRET_VALUE.search(value):
            problems.append(f"OcrTaskDefinition: environment variable {name} contains secret-looking data")

    # -- network: public subnets, no NAT, no inbound ------------------------------------------
    group = resources.get("OcrSecurityGroup", {}).get("Properties", {})

    if group.get("SecurityGroupIngress"):
        problems.append("OcrSecurityGroup: must have no inbound rule")

    egress = group.get("SecurityGroupEgress") or []

    if not egress or any(rule.get("FromPort") not in (443, 5432) or rule.get("ToPort") not in (443, 5432) or rule.get("IpProtocol") != "tcp" for rule in egress):
        problems.append("OcrSecurityGroup: outbound is limited to tcp 443 (HTTPS) and 5432 (PostgreSQL)")

    subnets = _resources(template, "AWS::EC2::Subnet")

    if len(subnets) < 2 or any(subnet["Properties"].get("MapPublicIpOnLaunch") is True for subnet in subnets.values()):
        problems.append("OCR subnets: at least two, with public IPs assigned only per task by the dispatcher")

    for kind in ("AWS::EC2::InternetGateway", "AWS::EC2::VPC"):
        if len(_resources(template, kind)) != 1:
            problems.append(f"expected exactly one {kind}")

    dispatcher_env = resources.get("DispatcherFunction", {}).get("Properties", {}).get("Environment", {}).get("Variables", {})

    for name in ("OCR_CLUSTER_ARN", "OCR_TASK_DEFINITION_ARN", "OCR_SUBNET_IDS", "OCR_SECURITY_GROUP_IDS", "JOBS_QUEUE_URL"):
        if name not in dispatcher_env:
            problems.append(f"DispatcherFunction: environment variable {name} is required")

    # -- dispatcher policy: narrow ECS permissions ---------------------------------------------
    roles = _resources(template, "AWS::IAM::Role")
    statements = _statements(roles["DispatcherRole"]) if "DispatcherRole" in roles else []
    run = [s for s in statements if "ecs:RunTask" in json.dumps(s["Action"])]

    if len(run) != 1 or not _mentions(run[0].get("Resource"), "task-definition/ap-agent-") or "ecs:cluster" not in json.dumps(run[0].get("Condition", {})):
        problems.append("DispatcherRole: ecs:RunTask must be limited to the OCR task definition and cluster")

    for action in ("ecs:DescribeTasks", "ecs:StopTask"):
        scoped = [s for s in statements if action in json.dumps(s["Action"])]

        if len(scoped) != 1 or not _mentions(scoped[0].get("Resource"), "OcrCluster"):
            problems.append(f"DispatcherRole: {action} must be limited to tasks of the OCR cluster")

    pass_role = [s for s in statements if "iam:PassRole" in json.dumps(s["Action"])]

    if len(pass_role) != 1 or "ecs-tasks.amazonaws.com" not in json.dumps(pass_role[0].get("Condition", {})) or not (
        _mentions(pass_role[0]["Resource"], "OcrExecutionRole") and _mentions(pass_role[0]["Resource"], "OcrTaskRole")
    ):
        problems.append("DispatcherRole: iam:PassRole must be limited to the two OCR roles, passed to ecs-tasks.amazonaws.com")

    execution = json.dumps(roles.get("OcrExecutionRole", {}).get("Properties", {}).get("Policies", []))

    if "repository/ap-agent-" not in execution or "/worker" not in execution:
        problems.append("OcrExecutionRole: image pull must be limited to the worker repository")

    return problems


def lambda_memory_problems(template: dict[str, Any], *, limit: int = LAMBDA_ACCOUNT_MEMORY_LIMIT_MB) -> list[str]:
    """Every Lambda memory size in `template` (resolved against parameter defaults) must be at most `limit` MB."""

    problems: list[str] = []
    parameters = template.get("Parameters", {})
    values: list[tuple[str, Any]] = [
        (name, body.get("Properties", {}).get("MemorySize"))
        for name, body in template["Resources"].items()
        if body.get("Type") in ("AWS::Serverless::Function", "AWS::Lambda::Function")
    ]
    globals_memory = template.get("Globals", {}).get("Function", {}).get("MemorySize")

    for name, value in values:
        value = globals_memory if value is None else value

        if isinstance(value, dict) and "Ref" in value:
            value = parameters.get(value["Ref"], {}).get("Default")

        if not isinstance(value, int) or value > limit:
            problems.append(f"{name}: Lambda memory {value!r} MB exceeds the {limit} MB limit")

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

    for name in ("template.yaml", "ecr.yaml"):
        text = (directory / name).read_text(encoding="utf-8")

        if SECRET_VALUE.search(text):
            problems.append(f"{name}: contains secret-looking data")

    if (directory / "fargate-fallback.yaml").exists():
        problems.append("fargate-fallback.yaml: obsolete (Fargate is the primary OCR runtime since M11E.2); remove it")

    return problems


def main() -> int:
    if "--lambda-memory-limit" in sys.argv:  # used by preflight/CI: python verify_aws_templates.py --lambda-memory-limit
        problems = lambda_memory_problems(load_template(AWS_DIR / "template.yaml"))
    else:
        problems = verify_all()

    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)

    print("AWS templates OK." if not problems else f"{len(problems)} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

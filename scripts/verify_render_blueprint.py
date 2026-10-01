#!/usr/bin/env python3
"""Static verification of the Render Blueprint (`render.yaml`) against the M11E
deployment rules. Exits non-zero and lists every violation. Needs PyYAML only.

Rules: exactly one public web service, one private service and one worker, one
instance each, explicit Dockerfiles that exist, no committed secret values, the
migration step and migration DSN only on the API service, no autoscaling, and
no service types the MVP forbids (no Redis/key-value, cron, database).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]

SECRET_NAME = re.compile(r"(SECRET|PASSWORD|TOKEN|DSN|ACCESS_KEY|PRIVATE|JWT_KEY|ENDPOINT_URL|BUCKET|ISSUER|PUBLISHABLE|AUTHORIZED_PARTIES|RUNTIME_ROLE)", re.I)
SECRET_VALUE = re.compile(r"(postgres(ql)?://|sk_(live|test)_|pk_(live|test)_|-----BEGIN|AKIA[0-9A-Z]{8,})")


def verify(blueprint_path: Path = REPO / "render.yaml") -> list[str]:
    problems: list[str] = []
    document = yaml.safe_load(blueprint_path.read_text(encoding="utf-8"))
    services = document.get("services", [])
    by_type: dict[str, list[dict]] = {"web": [], "pserv": [], "worker": []}

    for key in document:
        if key not in {"services", "envVarGroups"}:
            problems.append(f"unexpected top-level key: {key}")

    if "databases" in document or "envVarGroups" in document:
        problems.append("no databases or env-var groups: PostgreSQL stays on Neon and secrets are per service")

    for service in services:
        kind = service.get("type")

        if kind not in by_type:
            problems.append(f"service {service.get('name')!r} has forbidden type {kind!r}")
            continue

        by_type[kind].append(service)

    for kind, expected in (("web", 1), ("pserv", 1), ("worker", 1)):
        if len(by_type[kind]) != expected:
            problems.append(f"expected exactly {expected} {kind} service, found {len(by_type[kind])}")

    for service in services:
        name = service.get("name")

        if service.get("runtime") != "docker":
            problems.append(f"{name}: runtime must be docker")

        dockerfile = service.get("dockerfilePath", "")

        if not dockerfile or not (REPO / dockerfile).is_file():
            problems.append(f"{name}: dockerfilePath {dockerfile!r} does not exist")

        if service.get("numInstances") != 1:
            problems.append(f"{name}: numInstances must be exactly 1")

        for forbidden in ("scaling", "autoscaling"):
            if forbidden in service:
                problems.append(f"{name}: {forbidden} is not allowed in the MVP")

        if service.get("autoDeploy") is not False:
            problems.append(f"{name}: autoDeploy must be false (deploys are deliberate)")

        for variable in service.get("envVars", []):
            key = variable.get("key", "")
            has_value = "value" in variable

            if has_value and (SECRET_VALUE.search(str(variable["value"])) or (SECRET_NAME.search(key) and str(variable["value"]).strip())):
                problems.append(f"{name}: {key} carries a committed value; it must be sync: false")

            if not has_value and not any(k in variable for k in ("sync", "generateValue", "fromService")):
                problems.append(f"{name}: {key} has no source")

            if "sync" in variable and variable["sync"] is not False:
                problems.append(f"{name}: {key} sync must be false")

    web = (by_type["web"] or [{}])[0]
    api = (by_type["pserv"] or [{}])[0]
    worker = (by_type["worker"] or [{}])[0]

    def keys(service: dict) -> set[str]:
        return {variable.get("key") for variable in service.get("envVars", [])}

    if web.get("healthCheckPath") is None:
        problems.append("web: healthCheckPath is required")

    if "preDeployCommand" in web or "preDeployCommand" in worker:
        problems.append("only the API service runs the migration pre-deploy command")

    if "migrate.py" not in str(api.get("preDeployCommand", "")):
        problems.append("api: preDeployCommand must run scripts/migrate.py")

    for label, service in (("web", web), ("worker", worker)):
        if "AP_AGENT_POSTGRES_MIGRATION_DSN" in keys(service):
            problems.append(f"{label}: must not receive the migration DSN")

    for label, service in (("web", web), ("api", api)):
        if label == "web" and ("AP_AGENT_POSTGRES_DSN" in keys(service) or any(k.startswith("AP_AGENT_S3_") for k in keys(service))):
            problems.append("web: must not receive database or object-storage credentials")

    if "CLERK_SECRET_KEY" in keys(api) or "CLERK_SECRET_KEY" in keys(worker):
        problems.append("only the web service receives CLERK_SECRET_KEY")

    for service in (web, api, worker):
        environment = {v.get("key"): v.get("value") for v in service.get("envVars", [])}

        if environment.get("AP_AGENT_ENVIRONMENT") != "hosted":
            problems.append(f"{service.get('name')}: AP_AGENT_ENVIRONMENT must be hosted")

    if {v.get("key"): v.get("value") for v in web.get("envVars", [])}.get("AP_AGENT_FRONTEND_AUTH_MODE") != "clerk_jwt":
        problems.append("web: AP_AGENT_FRONTEND_AUTH_MODE must be clerk_jwt")

    if {v.get("key"): v.get("value") for v in api.get("envVars", [])}.get("AP_AGENT_AUTH_MODE") != "clerk_jwt":
        problems.append("api: AP_AGENT_AUTH_MODE must be clerk_jwt")

    for label, service in (("api", api), ("worker", worker)):
        if {v.get("key"): v.get("value") for v in service.get("envVars", [])}.get("AP_AGENT_ARTIFACT_STORAGE") != "s3":
            problems.append(f"{label}: AP_AGENT_ARTIFACT_STORAGE must be s3")

    if any("AP_AGENT_DEV_" in str(key) for service in services for key in keys(service)):
        problems.append("no AP_AGENT_DEV_* prototype identity variables")

    if not any(v.get("key") == "AP_AGENT_API_HOSTPORT" and "fromService" in v for v in web.get("envVars", [])):
        problems.append("web: the API address must come from fromService (private network)")

    if "healthCheckPath" in worker:
        problems.append("worker: has no HTTP health check")

    return problems


def main() -> int:
    problems = verify()

    if problems:
        print("render.yaml violates the M11E deployment rules:", file=sys.stderr)

        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)

        return 1

    print("render.yaml: OK (1 web, 1 private API, 1 worker; no committed secrets).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Stub `aws` / `sam` / `docker` executables for testing the deploy/aws shell scripts without AWS (M11E.2).

Every call is logged (one JSON argv list per line); the answer is the first rule whose `args` substrings all occur in the
joined argument list. Unmatched calls succeed with no output. Nothing here touches a network."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

STUB = """#!{python}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps([os.path.basename(sys.argv[0])] + argv) + "\\n")
joined = " ".join(argv)
if "--password-stdin" in argv:
    sys.stdin.read()  # consume the piped login token (never echoed)
for rule in json.load(open(os.environ["STUB_RULES"])):
    if rule.get("tool", os.path.basename(sys.argv[0])) == os.path.basename(sys.argv[0]) and all(part in joined for part in rule["args"]):
        if "write_last_arg" in rule:  # `aws lambda invoke --payload <json> <outfile> ...`: the response body goes to <outfile>
            open(argv[argv.index("--payload") + 2], "w").write(rule["write_last_arg"])
        if os.path.basename(sys.argv[0]) == "curl" and "-o" in argv:  # `curl -o <file> -w <fmt>`: body to the file, status to stdout
            open(argv[argv.index("-o") + 1], "w").write(rule.get("body", ""))
        sys.stdout.write(rule.get("out", ""))
        sys.stderr.write(rule.get("err", ""))
        sys.exit(rule.get("rc", 0))
"""


class StubAws:
    def __init__(self, directory: Path, rules: list[dict]) -> None:
        self.directory = directory
        self.bin = directory / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.log = directory / "calls.log"
        self.rules_path = directory / "rules.json"
        self.rules_path.write_text(json.dumps(rules))
        self.log.write_text("")

        for tool in ("aws", "sam", "docker", "curl"):
            path = self.bin / tool
            path.write_text(STUB.format(python=sys.executable))
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def environment(self, base: dict[str, str], **extra: str) -> dict[str, str]:
        import os

        return {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.directory),
            "STUB_LOG": str(self.log),
            "STUB_RULES": str(self.rules_path),
            "AWS_REGION": "eu-west-2",
            "STATE_DIR": str(self.directory / "state"),
            **base,
            **extra,
        }

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def calls_of(self, tool: str, *words: str) -> list[list[str]]:
        return [call for call in self.calls() if call[0] == tool and all(word in call for word in words)]

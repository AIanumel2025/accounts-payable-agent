"""M10 real-Uvicorn acceptance test (task §16): starts an actual local
`uvicorn` process on `127.0.0.1` (an available port), waits for
readiness, exercises health/dashboard/queue/detail/one command request
over real HTTP, then stops the process cleanly and leaves nothing running.

Deliberately does **not** use `uvicorn ap_agent.api.app:create_app
--factory` (which would call `create_app()` with no arguments, reading the
*production* `AP_AGENT_POSTGRES_DSN` environment variable): this suite
must only ever point at the isolated `AP_AGENT_TEST_POSTGRES_DSN` /
`ap_agent_m8_test` database, so it launches a small bootstrap script that
calls `create_app(dsn=..., memory_config=..., api_config=...)` explicitly
before handing the resulting app to `uvicorn.run`.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
import uuid

import pytest

from ap_agent.models.interface import interface_utc_now

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_BOOTSTRAP_SCRIPT = textwrap.dedent(
    """
    import sys
    import uvicorn

    from ap_agent.api.app import create_app
    from ap_agent.api.config import ApiConfig
    from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy

    dsn = sys.argv[1]
    port = int(sys.argv[2])

    app = create_app(
        dsn=dsn,
        memory_config=MemoryConfig(
            transport_policy=PostgresTransportPolicy(require_ssl=True, require_channel_binding=True)
        ),
        api_config=ApiConfig(enable_review_command_writes=True),
    )

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    """
)


def _wait_for_readiness(base_url: str, *, timeout_seconds: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass

        time.sleep(0.2)

    raise TimeoutError(f"Uvicorn server did not become ready within {timeout_seconds}s.")


def test_real_uvicorn_process_serves_the_review_api(runtime_dsn, local_config, tenant_id, seeded_case):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    process = subprocess.Popen(
        [sys.executable, "-c", _BOOTSTRAP_SCRIPT, runtime_dsn, str(port)],
        cwd="src",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        _wait_for_readiness(base_url)

        with urllib.request.urlopen(f"{base_url}/health", timeout=5) as response:
            assert response.status == 200
            health_body = json.loads(response.read())
            assert health_body["data"]["payment_execution"] == "PROHIBITED"

        headers = {
            "X-Tenant-ID": str(tenant_id),
            "X-Actor-ID": "auditor-1",
            "X-Actor-Role": "READ_ONLY_AUDITOR",
            "X-Authenticated-At": interface_utc_now().isoformat(),
        }

        dashboard_request = urllib.request.Request(f"{base_url}/api/v1/dashboard", headers=headers)
        with urllib.request.urlopen(dashboard_request, timeout=5) as response:
            assert response.status == 200
            dashboard_body = json.loads(response.read())
            assert dashboard_body["data"]["open_review_cases"] == 1

        queue_request = urllib.request.Request(f"{base_url}/api/v1/review-cases", headers=headers)
        with urllib.request.urlopen(queue_request, timeout=5) as response:
            assert response.status == 200
            queue_body = json.loads(response.read())
            assert queue_body["data"]["pagination"]["total_count"] == 1

        detail_request = urllib.request.Request(
            f"{base_url}/api/v1/review-cases/{seeded_case.review_case_id}", headers=headers
        )
        with urllib.request.urlopen(detail_request, timeout=5) as response:
            assert response.status == 200
            detail_body = json.loads(response.read())
            assert detail_body["data"]["review_case_id"] == str(seeded_case.review_case_id)

        operator_headers = dict(headers, **{"X-Actor-ID": "ap-operator-1", "X-Actor-Role": "AP_OPERATOR"})
        command_body = {
            "command_id": str(uuid.uuid4()),
            "idempotency_key": "m10-uvicorn-claim-v1",
            "action": "CLAIM",
            "disposition": None,
            "observed_review_revision": 1,
            "observed_workflow_revision": 1,
            "reason_codes": [],
            "notes": None,
            "corrections": [],
            "requested_at": interface_utc_now().isoformat(),
        }
        command_request = urllib.request.Request(
            f"{base_url}/api/v1/review-cases/{seeded_case.review_case_id}/commands",
            data=json.dumps(command_body).encode("utf-8"),
            headers={**operator_headers, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(command_request, timeout=5) as response:
            assert response.status == 200
            result_body = json.loads(response.read())
            assert result_body["status"] == "ACCEPTED"
            assert result_body["data"]["resulting_case_status"] == "IN_REVIEW"

    finally:
        process.terminate()

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

        assert process.poll() is not None, "Uvicorn subprocess did not stop cleanly."

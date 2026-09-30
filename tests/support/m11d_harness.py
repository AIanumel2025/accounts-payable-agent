"""Shared helpers for the M11D real-PostgreSQL acceptance tests."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi.testclient import TestClient

from ap_agent.adapters.reference_data_adapter import load_reference_data_bundle
from ap_agent.api.config import ApiConfig
from ap_agent.artifacts.storage import LocalFilesystemArtifactStore
from ap_agent.config.postgres import MemoryConfig
from ap_agent.models.interface import InterfaceRole, interface_utc_now
from ap_agent.models.operations import WorkflowJob
from ap_agent.repositories.operations_repository import OperationsRepository
from ap_agent.services.document_executor import DocumentJobExecutor
from ap_agent.services.resume_executor import ResumeJobExecutor
from ap_agent.worker.runner import WorkerRunner
from tests.support.m11d_seed import REFERENCE_DATA_DIRECTORY

REVIEWER = "m11d-reviewer-1"
OPERATOR = "m11d-operator-1"


def auth_headers(tenant_id: uuid.UUID, actor_id: str, role: InterfaceRole) -> dict[str, str]:
    return {
        "X-Tenant-ID": str(tenant_id),
        "X-Actor-ID": actor_id,
        "X-Actor-Role": role.value,
        "X-Authenticated-At": (interface_utc_now() - timedelta(seconds=5)).isoformat(),
    }


def make_client(runtime_dsn: str, config: MemoryConfig, artifact_root: Path, *, writes: bool = True) -> TestClient:
    from ap_agent.api.app import create_app

    return TestClient(
        create_app(
            dsn=runtime_dsn,
            memory_config=config,
            api_config=ApiConfig(
                enable_operations=True, artifact_root=artifact_root, enable_review_command_writes=writes
            ),
        )
    )


class Db:
    """Direct, tenant-scoped PostgreSQL inspection (runtime role, RLS on)."""

    def __init__(self, dsn: str, config: MemoryConfig, tenant_id: uuid.UUID) -> None:
        self._dsn = dsn
        self._config = config
        self.tenant_id = tenant_id

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        from ap_agent.db.connection import open_connection, set_tenant_context

        with open_connection(self._dsn, self._config) as connection:
            with connection.cursor() as cursor:
                set_tenant_context(cursor, str(self.tenant_id))
                cursor.execute(sql, params)
                return cursor.fetchall()

    def original_memory(self, workflow_id) -> tuple[str, str]:
        """(payload hash, canonical whole-row text) of the append-only original."""

        row = self.query(
            "SELECT payload_sha256, normalized_invoice::text, financial_validation::text, matching_result::text, "
            "matched_reference_data::text, review_required::text, array_to_string(review_reasons, ',') "
            "FROM ap_agent.invoice_memory_records WHERE tenant_id = %s AND workflow_id = %s;",
            (self.tenant_id, workflow_id),
        )[0]
        return row[0], json.dumps(row[1:], sort_keys=True)

    def versions(self, workflow_id) -> list[tuple]:
        return self.query(
            "SELECT version_id, derived_version, restart_stage, terminal_status, review_required, new_review_id, "
            "payload_sha256, normalized_invoice, parent_memory_version_id FROM ap_agent.invoice_memory_versions "
            "WHERE tenant_id = %s AND workflow_id = %s ORDER BY created_at;",
            (self.tenant_id, workflow_id),
        )

    def workflow(self, workflow_id) -> tuple:
        return self.query(
            "SELECT current_phase, current_status, review_required, lock_version FROM ap_agent.workflow_instances "
            "WHERE tenant_id = %s AND workflow_id = %s;",
            (self.tenant_id, workflow_id),
        )[0]

    def cases(self, workflow_id) -> list[tuple]:
        return self.query(
            "SELECT review_id, review_status, resolution_code, memory_version_id, source_resume_plan_id, reason_codes "
            "FROM ap_agent.review_cases WHERE tenant_id = %s AND workflow_id = %s ORDER BY opened_at, review_id;",
            (self.tenant_id, workflow_id),
        )

    def jobs(self, workflow_id=None) -> list[tuple]:
        return self.query(
            "SELECT job_id, job_type, status, error_code, current_stage FROM ap_agent.workflow_jobs "
            "WHERE tenant_id = %s AND (%s::uuid IS NULL OR workflow_id = %s::uuid) ORDER BY created_at;",
            (self.tenant_id, workflow_id, workflow_id),
        )

    def audit_types(self, workflow_id) -> list[str]:
        return [
            row[0]
            for row in self.query(
                "SELECT event_type FROM ap_agent.audit_events WHERE tenant_id = %s AND workflow_id = %s "
                "ORDER BY sequence_number;",
                (self.tenant_id, workflow_id),
            )
        ]

    def count(self, table: str) -> int:
        return int(self.query(f"SELECT COUNT(*) FROM ap_agent.{table} WHERE tenant_id = %s;", (self.tenant_id,))[0][0])


class ReviewApi:
    """The M11C review command API, as a human reviewer would use it."""

    def __init__(self, client: TestClient, tenant_id: uuid.UUID, case_id: uuid.UUID, actor_id: str = REVIEWER,
                 role: InterfaceRole = InterfaceRole.AP_REVIEWER) -> None:
        self.client = client
        self.tenant_id = tenant_id
        self.case_id = case_id
        self.actor_id = actor_id
        self.role = role
        self._counter = 0

    def _key(self, label: str) -> str:
        self._counter += 1
        return f"m11d-{label}-{self.case_id.hex[:8]}-{self._counter}"

    def caps(self) -> dict:
        response = self.client.get(
            f"/api/v1/review-cases/{self.case_id}/command-capabilities",
            headers=auth_headers(self.tenant_id, self.actor_id, self.role),
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def body(self, action: str, *, key: Optional[str] = None, **overrides: Any) -> dict:
        caps = self.caps()
        disposition = {"ACCEPT": "APPROVED", "CORRECT": "CORRECTED", "REJECT": "REJECTED"}.get(action)
        body = {
            "command_id": str(uuid.uuid4()),
            "idempotency_key": key or self._key(action.lower()),
            "action": action,
            "disposition": disposition,
            "observed_review_revision": caps["review_revision"],
            "observed_workflow_revision": caps["workflow_revision"],
            "reason_codes": [] if action in {"CLAIM", "RELEASE"} else ["REVIEWER_DECISION"],
            "notes": "Reviewed." if action == "REJECT" else None,
            "corrections": [],
            "requested_at": interface_utc_now().isoformat(),
        }
        body.update(overrides)
        return body

    def send(self, body: dict, *, expect: int = 200) -> dict:
        response = self.client.post(
            f"/api/v1/review-cases/{self.case_id}/commands",
            headers=auth_headers(self.tenant_id, self.actor_id, self.role),
            json=body,
        )
        assert response.status_code == expect, response.text
        return response.json()

    def command(self, action: str, *, key: Optional[str] = None, expect: int = 200, **overrides: Any) -> dict:
        return self.send(self.body(action, key=key, **overrides), expect=expect)

    def claim(self) -> dict:
        return self.command("CLAIM")

    def approve(self) -> dict:
        return self.command("ACCEPT")

    def correct(self, corrections: list[dict]) -> dict:
        return self.command("CORRECT", corrections=corrections)

    def resume(self, *, disposition: str, key: Optional[str] = None, expect: int = 200) -> dict:
        return self.command("RESUME_WORKFLOW", key=key, disposition=disposition, expect=expect)

    def evidence_id(self) -> str:
        return self.caps()["correction_policy"]["evidence_reference_ids"][0]


def make_runner(
    runtime_dsn: str,
    config: MemoryConfig,
    *,
    tenant_id: uuid.UUID,
    work_directory: Path,
    artifact_root: Optional[Path] = None,
    registry_builder: Optional[Callable[[WorkflowJob], Any]] = None,
) -> WorkerRunner:
    operations = OperationsRepository(runtime_dsn, config)
    store = LocalFilesystemArtifactStore(artifact_root or (work_directory / "artifacts"))

    def unavailable(_job: WorkflowJob):
        raise AssertionError("No document registry was configured for this test.")

    return WorkerRunner(
        operations=operations,
        document_executor=DocumentJobExecutor(
            operations=operations, artifact_store=store, registry_builder=registry_builder or unavailable
        ),
        resume_executor=ResumeJobExecutor(
            dsn=runtime_dsn,
            memory_config=config,
            operations=operations,
            reference_data=load_reference_data_bundle(REFERENCE_DATA_DIRECTORY),
            phase_directory_for=lambda job: work_directory / "phases" / job.job_id.hex,
            ocr_provider_name="tesseract",
            artifact_store=store,
        ),
        tenant_scope=tenant_id,
    )

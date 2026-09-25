#!/usr/bin/env python3
"""Optional memory smoke-test command (task §9): register a disposable
tenant, persist and retrieve one synthetic workflow record through
`PostgresMemoryRepository`, and verify it round-trips. Never touches
`invoice_memory_records` fixture content; uses only synthetic data
generated at runtime. Exits non-zero on any failure.

Usage:
    python scripts/memory_smoke_test.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ap_agent.config.postgres import MemoryConfig, PostgresTransportPolicy
from ap_agent.db.connection import load_dsn
from ap_agent.exceptions import PostgresConfigurationError
from ap_agent.models.memory import (
    MemoryWorkflowStage,
    MemoryWorkflowStatus,
    WorkflowMemoryRecord,
)
from ap_agent.repositories.postgres_memory_repository import PostgresMemoryRepository


def main() -> int:
    config = MemoryConfig(transport_policy=PostgresTransportPolicy(require_ssl=False))

    try:
        dsn = load_dsn(config)
    except PostgresConfigurationError as exc:
        print(f"Runtime DSN error: {exc}", file=sys.stderr)
        return 2

    repository = PostgresMemoryRepository(dsn, config)

    tenant_id = uuid4()
    repository.register_tenant(
        tenant_id=tenant_id,
        tenant_key=f"smoke-{tenant_id.hex[:8]}",
        display_name="Smoke Test Tenant",
    )

    document_id = uuid4()
    now = datetime.now(timezone.utc)

    record = WorkflowMemoryRecord(
        memory_id=uuid4(),
        tenant_id=str(tenant_id),
        batch_id=uuid4(),
        document_id=document_id,
        source_name="smoke-test.png",
        source_document_sha256="0" * 64,
        current_stage=MemoryWorkflowStage.REFERENCE_MATCHING,
        current_status=MemoryWorkflowStatus.SUCCEEDED,
        review_required=False,
        review_reasons=(),
        latest_matching_result_id=None,
        revision=1,
        created_at=now,
        updated_at=now,
    )

    stored = repository.create_or_get_workflow(tenant_id=tenant_id, record=record)
    fetched = repository.get_workflow_by_document(tenant_id=tenant_id, document_id=document_id)

    if fetched is None or fetched.memory_id != stored.memory_id:
        print("FAIL: workflow record did not round-trip.", file=sys.stderr)
        return 1

    print("PASS: tenant registered, workflow persisted and retrieved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

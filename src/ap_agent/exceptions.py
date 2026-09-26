"""Shared exceptions for the accounts payable agent.

Source: notebook cell 10 (Phase 1 contracts), active-definition table
§2 `exceptions.py`.

`NormalizationIntegrityError` is new in M5: it did not exist in the
notebook (the Phase 3 -> Phase 4 bridge was inline orchestration code
there, not a reusable typed function), but the M5 task brief requires the
modular typed bridge to fail closed on an integrity failure rather than
silently continuing or raising a bare `ValueError` like the notebook's
`normalize_invoice_document` does internally. See
`ap_agent.tools.normalization.build_normalization_input`.

`FinancialValidationIntegrityError` is new in M6, the same category of
addition for the Phase 4 -> Phase 5 bridge
(`ap_agent.tools.financial_validation.build_validation_input`) and for the
Phase 5 idempotent-persistence collision guard
(`ap_agent.tools.financial_validation.persist_financial_validation_result`).
The notebook's own cell-69 `build_validation_input` raised a bare
`ValueError` for its four identity checks; this module keeps those checks
but fails closed with this structured exception instead, and adds the
artifact-existence/hash check the task brief requires (M6 task §3) that the
notebook itself never performed.

`PostgresConfigurationError`, `MigrationIntegrityError`,
`TenantContextError` and `MemoryIntegrityError` are new in M8, for the
Phase 7 PostgreSQL operational-memory system
(`ap_agent.db`, `ap_agent.repositories`, `ap_agent.services.memory_service`).
The notebook's own Phase 7 cells raised bare `RuntimeError` for every one
of these cases (missing/invalid DSN, migration checksum or description
mismatch, unapplied tenant context, workflow-identity or payload-hash
collision); this module upgrades them to structured, typed exceptions
following the same precedent as `NormalizationIntegrityError` /
`FinancialValidationIntegrityError` / `MatchingIntegrityError`, without
changing which cases fail closed.

`PrivilegedRoleError` is new in M9's PostgreSQL-acceptance-workflow fix
(`ap_agent.db.roles.create_least_privilege_role`): raised when a runtime
role that already exists (from an earlier CI run against the same
persistent test database) is found to carry `SUPERUSER`, `BYPASSRLS`,
`CREATEDB`, `CREATEROLE`, or any other privilege beyond the expected
least-privilege set. The role is never "fixed" automatically -- a role
administrator without `SUPERUSER` cannot alter `SUPERUSER`/`BYPASSRLS`
even to reassert their current value (`ALTER ROLE ... NOSUPERUSER` fails
with `psycopg.errors.InsufficientPrivilege` unless the caller is itself a
superuser), so silently retrying or downgrading the role is not an option;
this fails closed instead.
"""

from typing import Any

from ap_agent.models.ingestion import IngestionErrorCode

__all__ = [
    "IngestionValidationError",
    "NormalizationIntegrityError",
    "FinancialValidationIntegrityError",
    "MatchingIntegrityError",
    "PostgresConfigurationError",
    "MigrationIntegrityError",
    "TenantContextError",
    "MemoryIntegrityError",
    "PrivilegedRoleError",
]


class IngestionValidationError(Exception):
    """Structured failure raised when document ingestion cannot continue."""

    def __init__(
        self,
        code: IngestionErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }


class NormalizationIntegrityError(Exception):
    """Raised when the Phase 3 -> Phase 4 bridge cannot verify that the OCR
    result it was given is the correct, current, persisted, post-guard
    result for the expected batch and document (M5 task §3). Fail closed:
    the caller must not proceed to normalisation."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class FinancialValidationIntegrityError(Exception):
    """Raised when the Phase 4 -> Phase 5 bridge cannot verify that the
    normalized invoice it was given is the correct, current, persisted
    result for the expected batch and document (M6 task §3), or when Phase
    5 artifact persistence detects a different-bytes collision on rerun
    (M6 task §16). Fail closed: the caller must not proceed."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class MatchingIntegrityError(Exception):
    """Raised when the Phase 4/5 -> Phase 6 bridge cannot verify that the
    normalized invoice and financial-validation result it was given belong
    to the same batch, document and source bytes (M7 task §3.G), or when
    Phase 6 artifact persistence detects a different-bytes collision on
    rerun (M7 task §5.I). Fail closed: the caller must not proceed to
    matching. The notebook's own `build_matching_input` (cell 82) and
    `write_text_idempotently` (cell 82) raised bare `ValueError`/
    `RuntimeError` for these cases; this mirrors the
    `NormalizationIntegrityError`/`FinancialValidationIntegrityError`
    precedent of upgrading them to a structured exception without changing
    which cases fail."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class PostgresConfigurationError(Exception):
    """Raised when the Phase 7 PostgreSQL DSN or transport policy cannot be
    satisfied: missing/empty/malformed DSN environment variable, or a DSN
    whose SSL/channel-binding/pooled-endpoint parameters fail the
    configured `ap_agent.config.postgres.PostgresTransportPolicy`. Never
    includes the DSN itself in its message (`ap_agent.db.connection.redact_dsn`)."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class MigrationIntegrityError(Exception):
    """Raised when an applied migration's recorded checksum or description
    does not match the migration file currently on disk (task §4A: "Reject
    altered checksums. Reject altered descriptions. Do not silently
    rewrite an applied migration."). The notebook's own
    `apply_bootstrap_migration` / `apply_memory_schema_migration` /
    `apply_invoice_memory_migration` raised a bare `RuntimeError` for this;
    `ap_agent.db.migration_runner` raises this structured exception
    instead, fail-closed, without applying any further migration."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class TenantContextError(Exception):
    """Raised when a tenant-scoped transaction cannot verify that
    `SELECT set_config('ap_agent.tenant_id', ...)` actually took effect
    (task §5: "Verify that the tenant context was applied ... Missing
    tenant context must fail closed"). Mirrors the notebook's
    `set_memory_tenant` (cell 88), which raised a bare `RuntimeError`."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class PrivilegedRoleError(Exception):
    """Raised when a PostgreSQL role that `ap_agent.db.roles
    .create_least_privilege_role` expected to be least-privilege already
    exists with `SUPERUSER`, `BYPASSRLS`, `CREATEDB`, `CREATEROLE`, or any
    other unexpectedly elevated attribute. Fail closed: the caller must
    not proceed to use this role for RLS-sensitive testing, and this
    module never attempts to silently downgrade it."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class MemoryIntegrityError(Exception):
    """Raised by `ap_agent.services.memory_service.MemoryService` and
    `ap_agent.repositories.postgres_memory_repository.PostgresMemoryRepository`
    when persisted content cannot be trusted: a workflow-identity
    collision, a stored-vs-recomputed payload-hash mismatch, a conflicting
    write under the same deterministic identity, or a Phase 4/5/6 result
    alignment failure (missing/duplicate/cross-document/cross-batch
    results). Mirrors the notebook's bare `RuntimeError`s in
    `persist_invoice_memory_records` / `retrieve_matched_invoice_memory`
    (cell 88) with a structured, fail-closed exception."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}

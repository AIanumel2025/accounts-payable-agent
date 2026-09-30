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

`ReviewCaseNotFoundError`, `TenantAccessDeniedError`,
`ReviewAuthenticationError`, `ReviewCommandRejectedError` and
`ReviewIntegrityError` are new in M10, for the Phase 9 human-review
interface (`ap_agent.repositories.review_repository`,
`ap_agent.services.review_queries`/`review_commands`/`review_decisions`/
`workflow_resume`, `ap_agent.api`). The notebook's Phase 9 cells raised a
bare `HTTPException`/`RuntimeError`/`ValueError` for every one of these
cases (unknown review case, cross-tenant access, malformed authentication,
a rejected/conflicting command, a payload-integrity or cross-tenant-leakage
failure detected server-side); this module upgrades them to structured,
typed exceptions so `ap_agent.api.errors` can map each one to the exact
HTTP status the M10 task brief §12 requires, without leaking SQL, stack
traces or DSNs into the response (task §9/§13/§21)."""

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
    "ReviewCaseNotFoundError",
    "TenantAccessDeniedError",
    "ReviewAuthenticationError",
    "ReviewCommandRejectedError",
    "ReviewIntegrityError",
    "OperationsRequestRejectedError",
    "ArtifactIntegrityError",
    "ResumeIntegrityError",
    "JobLeaseLostError",
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


class ReviewCaseNotFoundError(Exception):
    """Raised when a review case, or the workflow/invoice memory it must
    join against, cannot be found for the given tenant. Maps to HTTP 404
    (task §12). Never distinguishes "does not exist" from "exists under
    another tenant" in its message -- the tenant-scoped query itself
    (row-level security plus an explicit `tenant_id` predicate) already
    makes those indistinguishable, which is the desired fail-closed
    behaviour (task §5: "no cross-tenant reads")."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class TenantAccessDeniedError(Exception):
    """Raised when an authenticated actor's tenant does not match the
    tenant the request targets. Maps to HTTP 403 (task §12, notebook cell
    108's `authenticated_interface_actor`/`TENANT_ACCESS_DENIED`)."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class ReviewAuthenticationError(Exception):
    """Raised when the prototype header-authentication adapter
    (`ap_agent.api.dependencies`) cannot authenticate a request: a missing
    header, a malformed tenant/actor UUID, or an invalid/expired
    authentication timestamp. Maps to HTTP 401 (task §10/§12, notebook cell
    108's `authenticated_interface_actor`/`parse_authenticated_at`)."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class ReviewCommandRejectedError(Exception):
    """Raised when `ap_agent.services.review_commands.validate_review_command`
    (or the resume-specific `ap_agent.services.workflow_resume
    .validate_resume_command`) rejects a `ReviewCommand`, or when a
    transactional executor returns a non-accepted, non-idempotent
    `ReviewCommandResult`/`WorkflowResumeExecution`. Carries the same
    fail-closed error-code tuple the notebook's `validate_review_command`
    returns (cells 104/107); `ap_agent.api.errors` maps those codes to
    403/409/422 exactly as the notebook's own `command_http_error`
    (cell 108) does."""

    def __init__(self, errors: tuple[str, ...], message: str = "Review command rejected."):
        super().__init__(message)
        self.errors = errors
        self.message = message


class ReviewIntegrityError(Exception):
    """Raised when the Phase 9 review repository or services detect
    content that cannot be trusted: a cross-tenant row returned by a query
    that should have been tenant-scoped, a stored-vs-recomputed
    payload-hash mismatch, a malformed stored JSONB payload, or a
    cross-table consistency failure (review reasons/workflow status
    disagreeing between `ap_agent.review_cases` and
    `ap_agent.invoice_memory_records`). Mirrors the notebook's bare
    `RuntimeError`s in `retrieve_review_queue`/`retrieve_invoice_detail`/
    `retrieve_dashboard_record` (cells 101-103) with a structured,
    fail-closed exception; `ap_agent.api.errors` maps it to a redacted HTTP
    500 (task §12/§21: never leak SQL or payload contents to the client)."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


# ------------------------------------------------------------
# M11D (durable operations console and controlled workflow execution)
# ------------------------------------------------------------


class OperationsRequestRejectedError(Exception):
    """An operations-console request (upload submission) was refused
    before anything was stored or enqueued. Carries one stable,
    input-free error code (never the offending value) and the HTTP status
    `ap_agent.api.errors` should answer with."""

    def __init__(self, code: str, http_status: int = 422, message: str = "Operations request rejected."):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message


class ArtifactIntegrityError(Exception):
    """A stored upload artifact is missing, escapes the artifact root, or no
    longer hashes to the recorded SHA-256. Fails closed before any pipeline
    stage runs. `code` is a stable, path-free identifier."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ResumeIntegrityError(Exception):
    """A persisted resume plan, correction overlay, memory payload or
    identity failed verification, so the resume executor refuses to run any
    downstream stage. `code` is a stable, content-free identifier that is
    safe to store on a job and show in the UI."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class JobLeaseLostError(Exception):
    """A worker tried to heartbeat or finalize a job whose lease it no
    longer owns (expired and reclaimed, or already finalized)."""

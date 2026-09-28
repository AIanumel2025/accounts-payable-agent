# M11B — Review Queue and Invoice Detail Interface

## 1. Preflight

- Fetched `origin/main` and confirmed the working branch,
  `claude/elegant-goodall-1kwu0o` (the harness-designated branch — same
  deviation documented in M11A, §1 there), was already at exactly
  `origin/main`'s head: `fef3ee6854039cbec65fce1cf464bb304174abe5`
  ("M11A: human-review frontend foundation (#11)"), with a clean working
  tree.
- Confirmed M11A merged through that exact commit and read
  `docs/m11a_frontend_foundation_report.md` and
  `docs/m10_phase_9_review_api_report.md` before starting.
- Confirmed the required infrastructure exists: the M10 FastAPI
  application, `GET /api/v1/review-cases` (list) and
  `GET /api/v1/review-cases/{review_case_id}` (detail), M11A's frontend
  foundation (dashboard, application shell, formatting utilities, proxy
  route, acceptance-tenant seeding mechanism).
- Verified `AP_AGENT_TEST_POSTGRES_DSN`'s presence is expected in CI, not
  in this sandbox (no live PostgreSQL is reachable here — see §11).
- The notebook was not modified. No M11A safety guarantee was weakened:
  the proxy remains GET-only and allow-listed, `AP_AGENT_ENABLE_REVIEW_
  COMMAND_WRITES` is never referenced, and no command endpoint is ever
  called from any new code.
- **Branch:** work continued directly on `claude/elegant-goodall-1kwu0o`
  (restarted from `origin/main`, per the harness's own instruction for a
  merged designated branch) rather than a new `claude/m11b-*` branch —
  the same kind of documented deviation M11A itself recorded for its own
  branch naming.

## 2. Files changed

**Backend (`src/ap_agent/`):**
- `api/schemas.py` — `ApiEnvelope` became `Generic[T]`; added
  `HealthResponse`.
- `api/routes/dashboard.py`, `review_cases.py`, `health.py` —
  `response_model=ApiEnvelope[...]` per route; `review_commands.py` is
  unchanged (out of scope, still bare `ApiEnvelope`).

**Backend (test support, `tests/support/review_fixtures.py`):** added
`seed_named_review_case` and `NAMED_ACCEPTANCE_FIXTURE_KEYS`, plus the
per-fixture field/check/line-match baseline (§9).

**Backend (`scripts/manage_m11a_acceptance_tenant.py`):** the 3 generic
`seed_review_case` calls replaced with 3 `seed_named_review_case` calls;
seed output now also carries `review_case_ids`.

**CI:** `.github/workflows/m11a-frontend.yml` extended in place (§13) —
not duplicated.

**Frontend (`frontend/`):** see §4-§8 below for the new routes,
components, and libraries; `openapi/openapi.json` and
`src/types/api.generated.ts` regenerated from the new backend schema.

## 3. FastAPI contract investigation (before writing any UI code)

Read directly from `src/ap_agent/api/routes/review_cases.py` and
`src/ap_agent/api/schemas.py` — every parameter/enum used below is a
verified, existing one, never invented:

- `GET /api/v1/review-cases` query params: `page` (int, default 1),
  `page_size` (int, optional), `status` (plain `str`, not FastAPI-enum
  typed — see finding below), `assigned_to` (str), `priority`
  (`ReviewPriority` enum: `CRITICAL`/`HIGH`/`NORMAL`/`LOW`), `batch_id`
  (UUID).
- `GET /api/v1/review-cases/{review_case_id}`: path-only, no query
  params.
- **Finding — `status` filter only supports 4 of 6 `ReviewCaseStatus`
  values.** `status` is typed `Optional[str]` in FastAPI, but
  `ap_agent.services.review_queries.list_review_queue`'s
  `_DATABASE_REVIEW_STATUSES_BY_CASE_STATUS` maps only `OPEN`,
  `IN_REVIEW`, `RESOLVED`, `REJECTED`. Passing `AWAITING_INFORMATION` or
  `ESCALATED` raises a bare `ValueError` that isn't one of the typed
  domain exceptions the central handlers map, so it surfaces as an
  undifferentiated `500 INTERNAL_ERROR`, not a `422`. Independently, the
  database's `review_status` CHECK constraint itself can only ever hold
  `OPEN`/`CLAIMED`/`RESOLVED`/`CANCELLED` — so those two case-status
  values are currently unreachable as *data*, not just as a filter.
  **Resolution:** `SUPPORTED_QUEUE_STATUS_FILTERS` in
  `frontend/src/lib/api/review-queue-query.ts` exposes exactly the 4
  working values; the UI can never trigger the defect. Documented in
  code, not silently patched into the backend (out of this milestone's
  scope, and the task brief's own framing treats it as a UI concern).
- **Finding — `InvoiceDetailResponse` has no supplier/PO/goods-receipt
  resolution-status fields.** `ReviewQueueItem` (the *list* endpoint's
  response) carries `supplier_status`/`purchase_order_status`/
  `financial_validation_status`; `InvoiceDetailResponse` (the *detail*
  endpoint) does not — confirmed by reading both Pydantic models and
  their backing domain records (`ReviewQueueRecord` vs.
  `InvoiceDetailRecord`, `src/ap_agent/models/interface.py`). Section E
  of the detail page therefore shows the extracted supplier/PO *field
  values* instead of a resolution-status badge, with an explicit note
  pointing back to the queue for the status. See §7.
- **Finding — `TimelineEventResponse.event_type` values are
  `MemoryEventType` members, not workflow-lifecycle names.** Read from
  `ap_agent.repositories.mapping.domain_to_audit_event_row`: every
  `event_type` that ever reaches this field is one of `MEMORY_CREATED`,
  `WORKFLOW_TRANSITIONED`, `PHASE_RESULT_LINKED`,
  `REVIEW_DECISION_RECORDED`, `ARTIFACT_VERIFIED`,
  `MEMORY_CONFLICT_REJECTED` (`src/ap_agent/models/memory.py`). An
  earlier draft of `auditEventTypeLabel` in this same milestone used
  invented names (`WORKFLOW_STARTED`, `REVIEW_REQUIRED`, ...) before this
  was verified against the real mapping code; caught and corrected before
  merge (`frontend/src/lib/formatting/invoice.ts`,
  `frontend/tests/unit/invoice-formatting.test.ts`).

## 4. Server-side proxy boundary (extended, task §5)

`frontend/src/app/api/backend/[...path]/route.ts`'s flat string
allow-list became `matchAllowedPath(segments): AllowedPathMatch | null`,
matching by exact shape:

| Path | Method | Query params allowed |
|---|---|---|
| `health` | GET | none |
| `api/v1/dashboard` | GET | `batch_id` |
| `api/v1/review-cases` | GET | `page`, `page_size`, `status`, `assigned_to`, `priority`, `batch_id` |
| `api/v1/review-cases/{UUID}` | GET | none |

Only `GET` is exported (Next.js itself returns `405` for anything else —
verified by an HTTP-level test, not just documented). A malformed or
non-UUID id, a `/commands` subpath, an encoded traversal segment, or any
other path returns `404` before a request is ever built. Tenant/actor
identity is attached exclusively via
`buildDevelopmentAuthHeaders(config)`, server-side, from `loadServerEnvConfig()`
— never from a request header or query parameter (verified: `docs/
m11b_review_queue_detail_report.md` §12 below, and
`tests/e2e/proxy-security.spec.ts`).

## 5. `ApiEnvelope.data: Any` — fixed at the root

**Decision:** made `ApiEnvelope` generic (`class ApiEnvelope(_StrictModel, Generic[T])`,
`data: Optional[T] = None`) and every read route now declares
`response_model=ApiEnvelope[SomeResponse]`, instead of extending M11A's
hand-maintained shadow-interface workaround.

**Verification it changes no runtime behaviour:** compared
`ApiEnvelope(...).model_dump(mode="json")` for every route's real payload
before and after the change (`tests/unit/test_api_schemas.py`,
`tests/api/test_review_api_auth.py` — 18/18 pass locally, non-PostgreSQL
subset, verified in this sandbox); the JSON shape is byte-identical.
FastAPI now emits a distinct `ApiEnvelope_SomeResponse_` schema per
endpoint, so `openapi-typescript` generates a real type per endpoint —
`frontend/src/types/api-payloads.ts` is now a set of plain aliases into
the generated file, not a hand-maintained shape `api:check` couldn't
catch drift in. `review_commands.py` (bare `ApiEnvelope`) is untouched —
out of scope, and its `data` shape genuinely varies per command result in
a way that doesn't need this treatment yet.

## 6. Review queue page (`/review-queue`)

`frontend/src/app/review-queue/page.tsx` (Server Component,
`searchParams: Promise<...>`) + `QueueFilters`, `QueueResultsTable`,
`QueuePagination` (`frontend/src/components/review-queue/`).

- **Identity:** invoice number when present, else source filename
  (`primaryLabel`) — never the raw `review_case_id` UUID as the primary
  label.
- **Columns/cards:** status, priority, review reasons, assignment, batch
  (shortened, full value in `title`), updated (relative + `<time
  dateTime>` absolute).
- **Filters:** status (4 supported values only, §3), priority, assigned
  reviewer, batch UUID, clear-all — all URL-backed
  (`lib/api/review-queue-query.ts`), so a reload or back-navigation
  restores them exactly.
- **Pagination:** server-driven (`page`/`page_size` sent to FastAPI, not
  fetched-then-sliced client-side).
- **States:** loading (`loading.tsx`), populated, empty
  (no cases at all vs. "no matches for this filter" — distinct copy),
  partial/missing data (a missing field renders "—", never invented),
  malformed URL params / invalid batch UUID (dropped with a warning,
  never a broken page), backend timeout/unavailable/malformed response
  (`ErrorState` + retry), unauthorized/forbidden (same `ErrorState`
  mapping M11A already built).
- **Responsive:** a real `<table>` (≥768px, in a keyboard-scrollable
  wrapper so a very narrow tablet width never blows out the page) and an
  accessible card list below it — same fields on both, review reasons
  never hidden.
- **Accessibility:** one explicit `<Link aria-label="Open review case for
  ...">` per row/card — never a clickable `<tr>`.

## 7. Invoice/review-case detail page (`/review-cases/[reviewCaseId]`)

`frontend/src/app/review-cases/[reviewCaseId]/page.tsx` +
`frontend/src/components/review-detail/*Section.tsx`, composed by
`DetailBody.tsx`. Breadcrumb (`Breadcrumb.tsx`) links back to
`/review-queue` with whatever filters/page were active when the user left
it — the queue's own links append `?from=<url-encoded queue URL>`,
validated by `parseSafeReturnTo` (rejects anything not under
`/review-queue`, so a crafted `?from=` can never become an open redirect)
before being used as the breadcrumb's `href`.

Eight sections, each independently scoped so one missing/partial array
never blocks the others:

- **(A) Identity and status** — source name, workflow/stage/case-status
  badges, review-required, revision, review reasons, and all four ids
  (shortened display, full value via `title`).
- **(B) Normalized fields** — every `InterfaceFieldValueResponse`: raw
  value, normalized value (verbatim decimal text, never reformatted),
  type, confidence (low vs. missing visually distinguished, never
  collapsed to the same "—"), review-required, evidence-reference count.
  A missing value is always the explicit "—" marker, never `0`.
- **(C) Evidence references** — document checksum (SHA-256, safe to
  show), `original_document_uri` rendered as unavailable for as long as
  it is `null` (it always is in M11B — no document-serving capability
  exists yet), and every field's/check's `evidence_reference_ids` listed
  as opaque tokens grouped by source, plus a total count. No local path,
  credential, or signed/unsigned URL is ever rendered — there is nothing
  to render, since the contract has none of those.
- **(D) Financial validation** — every `FinancialCheckResponse`: type,
  status badge, message, expected/observed value, evidence count.
- **(E) Supplier and purchase-order matching** — see the §3 finding: no
  dedicated resolution-status field exists on this endpoint, so this
  section shows the extracted `SUPPLIER_NAME`/`SUPPLIER_ADDRESS`/
  `PURCHASE_ORDER_NUMBER` field values instead, with an explicit note
  rather than a fabricated status.
- **(F) Line matches** — per-line description/quantity/unit-price/
  line-total status badges and review reasons; an empty state (not a
  broken table) when there are none (e.g. `08181_warped`'s
  `purchase_order_status=NOT_REFERENCED`).
- **(G) Timeline** — real `audit_events` rows, in the exact order the
  backend returns them (never re-sorted), using the real `MemoryEventType`
  labels (§3).
- **(H) Previous review decisions** — historical, read-only; no replay,
  edit, or delete action exists anywhere on this page. An empty state for
  a fresh, undecided case.

**Error states:** an unknown or cross-tenant `review_case_id` and a
syntactically malformed one both render the same generic "Not found"
(`getReviewCaseDetail` pre-validates the UUID client-side before ever
calling the backend, so a malformed id never reaches FastAPI) — the 404
body never distinguishes the two, matching the backend's own behaviour
(`ReviewCaseNotFoundError`, task §14).

**Accessibility:** the three widest tables (normalized fields, financial
checks, line matches) are wrapped in a `role="region" tabIndex={0}
aria-label="..., scrollable"` container so a keyboard user can scroll them
horizontally on a narrow viewport without a mouse (WCAG 2.1.1/1.4.10) —
found and fixed during this milestone's own mobile screenshot review.

## 8. Dashboard integration (task §11)

`MetricCard` gained an optional `href`: the whole card becomes one link
(a single accessible name, not a name split across a wrapping link and an
inner labelled `<p>` — a duplicate-accessible-name bug caught by the
mocked Playwright suite and fixed before merge). Only two cards link
anywhere, and only to a real, exactly-supported filter:

- **"Review required"** → `/review-queue` (unfiltered; every
  review-required invoice has a corresponding review case).
- **"Open review cases"** → `/review-queue?status=OPEN` (`OPEN` is an
  exact, backend-supported `ReviewCaseStatus` value).

**"Unassigned review cases" is deliberately left unlinked**: `GET
/api/v1/review-cases` has no "unassigned" filter — only an exact
`assigned_to` reviewer-id match — so linking it would require inventing a
query parameter the backend doesn't support.

## 9. Controlled-fixture detail baseline (task §9)

The dashboard/queue baseline from M11A is unchanged and still holds
exactly: 4 total / 0 processing / 1 completed / 3 review-required / 0
failed invoices, 3 open / 3 unassigned review cases, reasons
`INHERITED_FINANCIAL_VALIDATION_REVIEW=3` / `SUPPLIER_NAME_MISSING=2` /
`INVOICE_LINE_TOTAL_MISSING=1`.

New per-fixture detail counts, every one cross-checked against real
Phase 1-8 golden baselines already in the repository — not invented:

| Fixture | Fields | Evidence refs | Financial checks | Line matches |
|---|---|---|---|---|
| `Template1_Instance90.jpg` | 9 | 18 | 12 | 5 |
| `08181_warped_document_perspective_shadow.jpg` | 6 | 25 | 12 | 0 |
| `invoice_Aaron Bergman_36258.pdf` | 10 | 22 | 8 | 1 |
| **Aggregate** | **25** | **65** | **32** | **6** |

**How each number was derived (`tests/support/review_fixtures.py`'s own
module comment has the full arithmetic):**
- **Fields (9/6/10):** the non-null values in
  `tests/golden/phase_4_expected_results.json`'s `expected_fields` for
  each document, matching `expected_header_field_count` exactly for two
  of three; Aaron Bergman's count includes one explicit null
  `SUPPLIER_NAME` row, consistent with its own `SUPPLIER_NAME_MISSING`
  review reason.
- **Financial checks (12/12/8):** 5 header-level check types
  (`INHERITED_REVIEW`, `REQUIRED_FINANCIAL_FIELDS`,
  `MONETARY_VALUE_VALIDITY`, `DATE_CONSISTENCY`, `CURRENCY_CONSISTENCY` —
  `phase_5_expected_results.json`'s `expected_header_checks`) + 1
  `LINE_ITEMS_TO_SUBTOTAL` + 1 `INVOICE_TOTAL_RECONCILIATION` + N
  `LINE_ITEM_ARITHMETIC` (one per line item, N = `phase_4`'s
  `expected_line_item_count`: 5/5/1). `5+1+1+5=12`, `5+1+1+5=12`,
  `5+1+1+1=8`. The individual PASSED/FAILED/REVIEW_REQUIRED/SKIPPED/
  NOT_APPLICABLE counts reproduce `phase_5_expected_results.json`'s own
  `expected_summary` exactly for all three documents (independently
  verified in this sandbox with a standalone script before writing the
  seeding code).
- **Line matches (5/0/1):** `tests/golden/phase_6_expected_results.json`'s
  own `line_matches` field per document, unchanged.
- **Evidence references (18/25/22) — the one number with no golden
  source.** The golden baselines predate the Phase 9 interface
  projection and never recorded per-field/-check evidence-reference
  counts. `_assign_evidence` in `review_fixtures.py` is a documented,
  fully deterministic scheme: one evidence reference for every
  successfully-evaluated field (non-null) or check
  (not `SKIPPED`/`NOT_APPLICABLE`), then extra references distributed
  round-robin over those same items (in declaration order) until the
  fixture's target total is reached. Verified by a standalone script in
  this sandbox to reproduce 18/25/22 exactly (and the 65 aggregate) before
  being wired into the seeding function.

## 10. Acceptance-tenant seeding (extended, task §10)

`scripts/manage_m11a_acceptance_tenant.py`'s `seed` subcommand now calls
`seed_named_review_case` for each of `NAMED_ACCEPTANCE_FIXTURE_KEYS`
(`"template1"`, `"08181_warped"`, `"aaron_bergman"`) instead of the
generic `seed_review_case` — the same isolated-tenant, least-privilege
role, fresh-UUID-per-run mechanism M11A built, extended rather than
duplicated. `seed_named_review_case` additionally seeds two real
`ap_agent.audit_events` rows per fixture via the repository's own
`append_audit_event` (never a raw INSERT), so the detail page's timeline
section has genuine content. The seed output JSON gained
`review_case_ids` (a map of fixture key → UUID) — not a secret, safe to
persist alongside the DSN, and read by `run-real-integration.mjs` to pass
the three ids through to the acceptance Playwright spec as
`AP_AGENT_ACCEPTANCE_REVIEW_CASE_IDS`.

M11A's own append-only-trigger finding is unchanged and still applies:
`cleanup` deletes only the tenant's `review_cases` rows;
`workflow_instances`/`invoice_memory_records`/`tenants` rows (now six per
run — 1 completed + 3 named fixtures' workflow rows, plus their
`invoice_memory_records`) remain permanently, by the schema's own
append-only-trigger and `ON DELETE RESTRICT` design, not a bug in this
script. Each run's tenant UUID is freshly generated
(`uuid.uuid4()`), so retries cannot collide with an earlier run's rows.

## 11. What was and was not verified in this sandbox

This development sandbox has no reachable PostgreSQL instance and a
minimal Python environment (installing `ap_agent[api,dev]` was possible;
`numpy`/`fastapi` are otherwise absent from the base image). What was
verified here, directly:

- **Backend:** `ApiEnvelope[T]` byte-identical-JSON claim (§5); the
  non-PostgreSQL subset of `tests/api/`/`tests/unit/test_api_schemas.py`
  (18/18 pass); the seeding module imports cleanly and its dataclasses/
  `MatchedInvoiceMemoryRecord`/`AuditMemoryEvent` construct without error;
  the exact field/evidence/check/line-match arithmetic (§9), reproduced
  standalone before being wired into `seed_named_review_case`.
- **Frontend:** full `npm run lint` / `typecheck` / `api:check` / `test`
  (159 unit+component tests) / `build` / `check-build-secrets`, and the
  full mocked Playwright suite (90 tests across dashboard, review queue,
  review case detail, navigation, accessibility, backend-failure,
  secret-exposure, and the new HTTP-level proxy-security suite) — all
  green, run against `tests/e2e/support/mock-backend.mjs`, never against a
  real database.
- **Not run in this sandbox (requires live PostgreSQL, as in M11A):**
  `tests/api/test_review_api_postgres.py`, the PostgreSQL-requiring cases
  in `test_review_api_auth.py`, and
  `tests/e2e/real-integration.spec.ts` end to end. These execute in CI's
  `real-integration` job, which has `AP_AGENT_TEST_POSTGRES_DSN`
  configured — the same limitation M8/M9/M10/M11A already documented for
  this environment (`docs/m10_phase_9_review_api_report.md` §13.2,
  `docs/m11a_frontend_foundation_report.md`).

## 12. Security (task §14)

- Browser-supplied `x-tenant-id`/`x-actor-id`/`x-actor-role` headers are
  never forwarded or trusted — verified with an HTTP-level test that sends
  bogus values for all three and still gets a normal `200` (proof the
  proxy attaches its own, real headers; the mock backend itself 401s
  without them).
- There is no tenant-identity query parameter anywhere in the allow-list
  for a caller to set in the first place (not merely filtered —
  unreachable).
- Every command subpath, every non-GET method, every malformed/non-UUID
  detail id, and an encoded traversal segment are all rejected with
  `404`/`405` before a request is built (`tests/e2e/proxy-security.spec.ts`,
  21 tests, HTTP-level, not just the existing unit-level
  `matchAllowedPath` tests).
- An unknown or cross-tenant review case returns a generic 404 whose body
  never mentions "tenant" — verified directly.
- No local filesystem path, DSN, hostname, or dev-only identifier appears
  in rendered HTML, browser JS state, storage, cookies, or console output
  on either new page (`tests/e2e/secret-exposure.spec.ts`, extended).
- No command/mutation request is ever issued while browsing either new
  page (`tests/e2e/review-case-detail.spec.ts`).

## 13. CI (task §19/§20)

`.github/workflows/m11a-frontend.yml` extended in place — the file was
not duplicated or renamed. Changes: `name:` now reads "M11A/M11B frontend
foundation"; `on.pull_request.paths` gained the new backend files this
milestone touches (`scripts/manage_m11a_acceptance_tenant.py`,
`review_repository.py`, `postgres_memory_repository.py`,
`tests/support/review_fixtures.py`); `build-and-test` gained a
non-PostgreSQL `tests/api/`/`test_api_schemas.py` step (since
`schemas.py` changed); `real-integration` gained the PostgreSQL-requiring
remainder of that same suite. Every existing step (lint, typecheck,
api:check, unit/component tests, build, mocked Playwright, secret scan,
real integration) now exercises both M11A's and M11B's code, since both
live in the same npm scripts and test directories — there are no separate
"M11A-only" and "M11B-only" steps to report.

**Process constraint (task §20) followed:** this report and the README
update below are written and committed together with the final code
change, in one push — not as a separate documentation-only follow-up
commit after code was already pushed, which is exactly the pattern that
retriggers this path-filtered workflow on a docs-only diff.

## 14. Visual verification

Desktop (1440px) and mobile (390px) screenshots were captured for the
dashboard (with the two new linked metric cards), the review queue
(populated, all three fixtures, filters), and all three named fixtures'
detail pages (all eight sections, including the zero-line-matches empty
state on `08181_warped` and the corrected real timeline labels). All
render cleanly with no layout overflow at any tested width.

## 15. Out of scope (confirmed absent)

No Payments route. No claim/release/correction/approve/reject/escalate/
resume/payment/ERP control anywhere. No command endpoint call from any
new code path. No document streaming, signed URL, or image overlay.
`AP_AGENT_ENABLE_REVIEW_COMMAND_WRITES` is never referenced.

## 16. Final assessment

- **Safe to merge:** yes, once CI is green on this PR's head, including
  the real-integration job (not verifiable in this sandbox — see §11).
- **M11C readiness:** the review-command endpoints already exist in M10's
  API surface (`review_commands.py`, task-brief-confirmed out of scope
  here); the detail page's read-only sections (fields, financial checks,
  line matches) are already shaped to host inline correction/decision
  controls in a future milestone without a data-layer rework, since
  `lib/api/review-cases.ts` and the section components take the same
  `InvoiceDetailPayload` a command-aware version would extend, not
  replace.

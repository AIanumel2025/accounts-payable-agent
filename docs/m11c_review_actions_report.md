# M11C — Interactive human-review actions

Status: implemented on the harness-authorised branch, PR open (not merged).
Scope: the controlled, opt-in review-action layer of the Next.js frontend and
the small backend additions it needed. **No payment execution, bank transfer,
ERP posting or automatic downstream pipeline execution exists anywhere.**

## 1. Preflight

| Item | Result |
|---|---|
| Starting `main` | `6454bd1cc7d885e635e7a9200842b67bc37da54f` (M11B, PR #12) — confirmed to be `origin/main` |
| Working tree | clean |
| Branch | `claude/fervent-lamport-m7kmax` |
| Notebook | unmodified |
| `AP_AGENT_TEST_POSTGRES_DSN` | present; parsed without printing; database name verified `ap_agent_m8_test` |

**Branch deviation.** The task asks for `claude/m11c-review-actions`. The
execution harness pins the session to `claude/fervent-lamport-m7kmax` and
forbids pushing elsewhere, so that branch is used and the deviation recorded.

**Environment finding (honest limitation).** The sandbox this was built in
cannot open raw TCP connections to the remote test database (the same egress
limit M10 §13.2 and M11A recorded). Everything below that says "real
PostgreSQL" was therefore executed **locally against a scratch PostgreSQL 16
cluster whose database is literally named `ap_agent_m8_test`**, configured
with TLS and SCRAM (so the repository's `require_ssl` /
`require_channel_binding` transport policy was exercised unchanged), a
migration-owner role and the same least-privilege runtime role mechanism the
M8–M10 suites use. The remote `ap_agent_m8_test` database is exercised **by CI
on the pull request** (§16); until that job is green, nothing here claims a
Neon result.

## 2. Command-contract audit

Read from `src/ap_agent/api/routes/review_commands.py`,
`services/review_commands.py`, `review_decisions.py`, `workflow_resume.py` and
`repositories/review_repository.py`. The frontend introduces **no** command
semantics of its own beyond a fail-fast mirror of the allow-list.

| Concern | Active behaviour (source of truth) |
|---|---|
| Actor / tenant | four `X-*` header adapter (`dependencies.py`); tenant and actor come only from those headers |
| Role policy | `default_interface_config().role_permissions`: `AP_REVIEWER`/`TENANT_ADMIN` all actions; `AP_OPERATOR` `CLAIM, RELEASE, CORRECT` + the four deferred ones; `READ_ONLY_AUDITOR` none |
| Claim ownership | every action except `CLAIM` needs status `IN_REVIEW` and `assigned_reviewer_id == actor` |
| Revisions | review revision = `1 + decision count`; workflow revision = `workflow_instances.lock_version`, bumped by claim/release/decision/resume. Claim/release do **not** bump the review revision |
| Idempotency | canonical SHA-256 fingerprint over the command content (incl. `command_id`); stored on the audit event; same key + same fingerprint → `IDEMPOTENT`, same key + different fingerprint → `CONFLICT` (409); looked up inside the locked transaction, before validation |
| Corrections | header fields must be present and `previous_value` must equal the stored normalized value; line fields need a known line number; reason and ≥1 evidence id required; evidence must be a subset of the case's evidence ids; duplicate targets rejected |
| Terminal decisions | `ACCEPT→APPROVED`, `CORRECT→CORRECTED`, `REJECT→REJECTED`; append-only `review_decisions` row + case resolved + workflow revision bump + audit event, one transaction |
| Resume | case `RESOLVED`, disposition `APPROVED`/`CORRECTED`, actor is the deciding reviewer, payload-hash integrity re-verified, stale revisions rejected; creates a plan + audit event and moves the workflow to the restart stage — **nothing is executed** |
| Restart stage | approved, no corrections → `MEMORY_PERSISTENCE`; any financial/line field → `FINANCIAL_VALIDATION`; supplier/PO fields → `REFERENCE_MATCHING`; other metadata → `FINANCIAL_VALIDATION` |
| Append-only | `review_decisions`, `audit_events`, `invoice_memory_records` refuse UPDATE/DELETE by trigger |

**Supported transactional actions:** `CLAIM`, `RELEASE`, `ACCEPT`, `CORRECT`,
`REJECT`, `RESUME_WORKFLOW`.
**Deferred (no executor; never exposed as a control):** `CONFIRM_SUPPLIER`,
`CONFIRM_PURCHASE_ORDER`, `REQUEST_INFORMATION`, `ESCALATE`. The backend still
authorises some of them by role and `POST`s for them return
`REVIEW_ACTION_NOT_IMPLEMENTED`; the frontend never sends them (the Next.js
boundary rejects them with `422`). `ReviewAction` contains no payment member.

## 3. Frontend command modes

`AP_AGENT_FRONTEND_REVIEW_COMMAND_MODE` — server-only, never `NEXT_PUBLIC_`.

| Value | Behaviour |
|---|---|
| `disabled` (**default**, also unset/blank) | M11B read-only behaviour; no capabilities call; no command forwarded even if the backend is in `COMMIT` |
| `validation_only` | forms work; backend must report `VALIDATION_ONLY`; result is `VALIDATED` and the UI says *"Validated — no database changes were made."*; resume hidden |
| `commit` | backend must report `COMMIT`; success re-reads PostgreSQL via `router.refresh()` |
| anything else | treated as `disabled`, flagged invalid, "misconfigured" banner, POSTs answered `403 COMMAND_MODE_MISCONFIGURED` |

Mismatch fails closed **twice**: the UI hides controls when the backend's
capability `command_mode` disagrees, and the POST boundary independently calls
`/health` and refuses (`409 COMMAND_MODE_MISMATCH`) before any command leaves
the server. Residual TOCTOU: a backend that is restarted in a different mode
between the health probe and the command is not detected per request; mode
changes require a process restart, so this is a deployment-time, not
runtime-user-driven, condition.

## 4. Authentication limitation

Still the M10 development-header adapter. It is **not production
authentication**. Write-enabled demonstrations use a generated, isolated
tenant; production needs OIDC/JWT or another signed identity mechanism; **M11C
is not approved for a public write-enabled deployment** using prototype
headers. Tenant, actor id, role and `X-Authenticated-At` are constructed only
inside the Next.js server from server-only configuration — never from browser
input — and the configured actor id / tenant UUID / role enum are not present
in browser assets (§14).

## 5. Typed command API contract

- `ValidationOnlyCommandResponse`, `CommandResultResponse` (committed and
  idempotent), `WorkflowResumeResponse`, `ApiErrorEnvelope` and the
  capability schemas are now real Pydantic models; the command route is
  `response_model=ApiEnvelope[Union[…]]` with 401/403/404/409/422/500/503
  documented as `ApiErrorEnvelope`.
- JSON is unchanged for existing fields; `WorkflowResumeResponse` gains one
  additive field, `resume_plan_id`.
- `frontend/openapi/openapi.json` and `src/types/api.generated.ts`
  regenerated; `npm run api:check` was shown to **fail** when the committed
  generated types were deliberately corrupted, then pass after restoring them.
- No `as` assertions on upstream command data: responses are narrowed from
  `unknown` by `parseCommandSuccess` / `parseCommandFailure` /
  `parseCapabilities`, which reject anything malformed.

## 6. Command-capability projection

**A capability endpoint was necessary.** The M11B detail payload lacks: the
workflow revision (required by every command), whether the case is assigned to
*the calling actor*, the exact stored header values (`previous_value` must
match them), known line numbers and current line values, the case's usable
evidence ids, and the role/mode-dependent list of currently eligible actions.
Duplicating that policy in TypeScript would drift; instead
`GET /api/v1/review-cases/{id}/command-capabilities` derives it from the same
`ReviewCommandContext`/`InterfaceConfig` the executors use.

It is advisory, read-only, exposes no tenant id, actor id, credential or
configuration value (asserted in tests), lists only supported actions, and is
called server-side from the detail page (no browser GET route was added).
Executors still re-validate everything in a locked transaction.

## 7. Server-side POST boundary

Exactly one browser-reachable write:
`POST /api/v1/review-cases/{uuid}/commands` (`app/api/v1/review-cases/[reviewCaseId]/commands/route.ts`,
exporting only `POST`; Next answers 405 for everything else). The read-only
`/api/backend/[...path]` proxy remains GET-only and still 404s command paths.
`handleCommandRequest` (unit-tested with injected collaborators) enforces, in
order: UUID path parameter → mode → same-origin/`Sec-Fetch-Site` → JSON content
type → CSRF token → 64 KiB streaming body cap → safe JSON parse → allow-listed
fields (tenant/actor/role/timestamps/mode/unknown fields rejected) → server
config → backend-mode agreement → server-built auth headers and `requested_at`
(taken after `X-Authenticated-At`) → upstream call with timeout. Upstream
failures are reduced to a whitelist of error **codes**; raw text, SQL, DSNs,
hostnames and paths cannot reach the browser.

## 8. CSRF protection

The app has no session cookie, so a cookie-bound token is unnecessary. Four
independent layers: (1) `Origin` present and host equals `Host`;
(2) `Sec-Fetch-Site` must be `same-origin` when sent; (3) `application/json`
only; (4) an `X-CSRF-Token` HMAC token bound to the review-case id with an
8-hour expiry, embedded in the server-rendered page (unreadable cross-origin)
and never stored in cookies or browser storage. The key is
`AP_AGENT_FRONTEND_CSRF_SECRET` (≥32 chars) or a per-process random key cached
on `globalThis` (Next bundles pages and route handlers separately). A
multi-instance deployment must set the secret.

## 9. Command identity and idempotency

One intended operation → one command UUID and one key
(`ap-ui-<uuid>`, satisfying `^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$`). An
in-memory `OperationIdentityTracker` reuses the pair for an *identical* retry
(same case, action, payload, observed revisions) and mints fresh identity for
any change — including refreshed revisions. A synchronous ref plus disabled
buttons stop double submission; success refresh is a `router.refresh()`, never
a re-POST; nothing is stored in browser storage. Verified in a real browser: a
response deliberately dropped after the server executed the claim is retried
with the same identity and comes back `IDEMPOTENT`; a reused key with different
content surfaces "Conflicting duplicate request" (409). A page reload starts a
new operation (identity is not persisted); the backend's revision checks make
that safe.

## 10. Behaviour, action by action

- **Claim** — shown only when permitted, open and unassigned; sends observed
  revisions; refreshes from PostgreSQL; a lost race yields a controlled 409.
- **Release** — only for the actor's own claim; asks for confirmation when an
  unsaved correction draft would be discarded.
- **Approve** — label "Approve", wire `ACCEPT`/`APPROVED`; confirmation dialog
  with required reason codes; corrections must be empty; says "Executed" only
  for a committed `ACCEPTED`, never for `VALIDATED`.
- **Correct** — editor limited to backend-listed header/line targets, immutable
  original value shown, exact-decimal validation (never `Number`), required
  reason (≥5 chars) and ≥1 evidence id chosen from the case's own references,
  multiple corrections, duplicate-target check, inline errors + focused error
  summary that keeps the form intact, confirmation summary
  (`previous → corrected`).
- **Reject** — required reason codes **and** notes, explicit "terminal for this
  review case" dialog, resume never offered.
- **Resume** — offered only in `commit` mode, for a resolved `APPROVED`/
  `CORRECTED` case whose deciding reviewer is the actor and that has no earlier
  request; sends the stored disposition, no corrections. Result shows restart
  stage, derived version, decision id, resume-plan id and *"A controlled
  workflow-resume handoff was created. Downstream execution has not run yet."*
  Restart stages verified in a browser against PostgreSQL: approved →
  `MEMORY_PERSISTENCE`, `LINE_QUANTITY` → `FINANCIAL_VALIDATION`,
  `PURCHASE_ORDER_NUMBER` → `REFERENCE_MATCHING`.

## 11. Concurrency, staleness, authorization, tenancy (results)

| Property | Result |
|---|---|
| Concurrent claim | **Exactly one winner** in both a 4-thread real-PostgreSQL test (overlapping transactions on independent apps) and two real Chromium sessions clicking together; loser gets a controlled 409; DB shows one owner, one audit event, revision advanced once |
| Stale revision | `409`; UI locks submission until fresh data arrives, keeps form content, never retries silently; DB unchanged (asserted by full-tenant fingerprint) |
| Ownership conflict | other reviewer's `RELEASE/ACCEPT/REJECT/RESUME` → `409` (`CASE_ASSIGNED_TO_DIFFERENT_REVIEWER` / `DECISION_ACTOR_MISMATCH`) |
| Authorization | auditor → `403 ACTION_NOT_PERMITTED` even with a forged request and valid page token; operator limited to claim/release/correct |
| Tenant isolation | other tenant → `404` (UI page and direct API); other tenant's case untouched; row-level security proven invisible; interleaved-tenant reads never bleed |
| Original memory | payload hash **and** normalized invoice unchanged for every acted-on case (approve, both corrections, reject, resume) |
| Append-only decisions | one row per decision; identical retry writes nothing; `UPDATE`/`DELETE` on `review_decisions` and `audit_events` refused by the database |
| Append-only audit | one `INTERFACE_COMMAND_EXECUTED` per executed command, one `WORKFLOW_RESUME_REQUESTED` per handoff; none for rejections/idempotent retries |
| Rollback | injected failure after the decision insert → HTTP 500 (redacted), decision row and case/workflow state fully rolled back |
| Validation-only | no database mutation (full-tenant fingerprint equal), against both mocked and a **real** validation-only FastAPI |
| Payment prohibition | `EXECUTE_PAYMENT`, `RELEASE_PAYMENT`, `BANK_TRANSFER`, `POST_TO_ERP`, `PAY` and payment/bank/ERP fields rejected `422` by Pydantic before any executor, at the Next boundary and directly at FastAPI; `ReviewAction` has no payment member |

## 12. Deviations from the notebook / M10 behaviour (all tested)

| # | Change | Why |
|---|---|---|
| D-M11C-1 | `ReviewRepository.get_invoice_detail` accepts `RESOLVED`/`CANCELLED` cases and only requires `review_required` for active cases; `current_stage` is read from the workflow's phase (was a constant `HUMAN_REVIEW`) | M10/M11B could only read *active* cases (a decided case returned 500 "not active"), so a reviewer could not see their own decision or the resume handoff. Active-case invariants are unchanged |
| D-M11C-2 | `execute_resume_command`: a second resume under a *different* idempotency key now returns `409 WORKFLOW_NOT_AWAITING_RESUME` instead of a 500 integrity error | found by the new PostgreSQL test (two tabs / regenerated identity). Nothing had been written at that point |
| D-M11C-3 | `CASE_NOT_OPEN`, `CASE_ALREADY_ASSIGNED`, `CASE_NOT_CLAIMED`, `WORKFLOW_NOT_AWAITING_RESUME` map to `409` (were `422`) | task §17/§18: ownership/state conflicts are conflicts |
| D-M11C-4 | request-validation failures now return the standard error envelope with input-free codes (`REQUEST_VALIDATION_FAILED`, `REVIEW_ACTION_NOT_SUPPORTED`, `INVALID_FIELD:<path>`) instead of FastAPI's default body that echoes the offending input | task §18: never echo raw input |
| D-M11C-5 | additive `resume_plan_id` on `WorkflowResumeResponse`; new capability endpoint | task §15/§6 |
| — | `frontend/src/app/icon.svg` added | browsers requested `/favicon.ico` (404) on every fresh session, surfacing as a console error in acceptance runs |

## 13. Test results

Exact counts from this session (local scratch PostgreSQL 16 named
`ap_agent_m8_test` where a database is involved).

| # | Suite | Result |
|---|---|---|
| 1 | M10 command-policy unit tests (`test_review_commands_policy`, `test_review_decisions`, `test_workflow_resume_policy`) | passed (part of the 921 below) |
| 2 | M10 API command tests (`tests/api/`) | passed |
| 3 | M10 PostgreSQL command tests (`tests/integration/test_review_postgres_integration.py`, `tests/api/test_review_api_postgres.py`) | passed |
| — | **New** M11C backend: `tests/api/test_m11c_review_actions_postgres.py` (45 real-PostgreSQL scenarios) + `tests/unit/test_review_capabilities.py` + extended `test_api_errors.py` | passed |
| — | Backend total (`tests/unit`, `tests/api`, review/memory PostgreSQL integration) | **921 passed, 13 failed, 4 errors, 0 skipped, 0 deselected.** All 13 failures (`test_ocr_orchestration`, `ModuleNotFoundError: cv2`) and 4 collection errors (`test_paddleocr_adapter`, `test_preprocessing_*`, `test_tesseract_adapter`: `numpy`/PaddleOCR) are environmental — the 13 fail identically on the untouched baseline (verified with `git stash`). The OCR regression workflow was intentionally **not** run: this milestone does not change OCR behaviour |
| 4 | Existing M11A/M11B unit + component tests | 159 passed |
| 5 | **New** M11C unit tests | 199 passed (mode parsing, contract allow-list, identity, corrections, messages, capabilities, CSRF, boundary) |
| 6 | **New** M11C component tests | 46 passed |
| — | Vitest total | **404 passed** (33 files); ESLint and `tsc --noEmit` clean |
| 7 | Existing M11A/M11B Playwright (mocked) | **90 passed** (unchanged count; re-run after all M11C changes) |
| 8 | **New** M11C mocked Playwright | **48 passed** (workspace 26, security 15, accessibility 7) |
| 9 | Production build | succeeds; routes: `/api/v1/review-cases/[reviewCaseId]/commands` (dynamic), detail page dynamic |
| 10 | `npm run api:check` | OK; proven to fail on injected drift |
| 11 | Built-asset secret scan | 17 static files, no forbidden pattern (patterns extended for M11C variable names and dev actor ids) |
| 12 | **Real** FastAPI + PostgreSQL + Next.js, write-enabled | **15 browser scenarios passed + 57 direct database checks passed, 0 failed** |

The pre-existing timing-sensitive Uvicorn subprocess test
(`tests/api/test_review_api_uvicorn.py`) **passed locally** in this session but
remains excluded from the CI PostgreSQL step exactly as in M11B. It was not
investigated further because M11C did not change what it exercises in a way
that affects its timing; its exclusion is not broadened and it is not claimed
to pass in CI.

## 14. Secret exposure

Inspected in the browser: rendered HTML (before and after an action), every
downloaded `_next/static` script, `localStorage`, `sessionStorage`,
`document.cookie`, the cookie jar, and the console. None carries a DSN, the
configured tenant UUID, the command-mode variable, the CSRF secret or the
server-only role enum; the configured actor id is absent from static assets and
from pages loaded before an action (after an action it legitimately appears as
*data*, e.g. the timeline's "claimed by" row, exactly like decision reviewers
and assignees since M11B). The raw actor-role enum is stripped from the props
sent to the client (the browser gets only a label).

## 15. Accessibility and responsive results

- **axe-core (WCAG 2.0/2.1/2.2 A+AA tags)** on open, claimed, correction editor
  (valid and error states), approve dialog, disabled, validation-only,
  mismatch, read-only and mobile states: **no serious or critical violations**.
  It caught a real defect first — 24×24 px target size (WCAG 2.2 SC 2.5.8) on
  checkboxes — fixed.
- **Keyboard**: claim and approve done with the keyboard alone; error summary
  and result region receive focus and are live regions.
- **Dialogs**: native `<dialog>` modal; Escape closes; focus restored to the
  opener; focus lands on *Cancel* (a stray Enter never confirms a terminal
  action). The browser test found that a native modal lets Tab leave the
  document, so an explicit wrap-around focus trap was added and is asserted for
  14 Tab and 14 Shift+Tab presses.
- **Responsive**: 390 px viewport — no horizontal overflow, dialog within the
  viewport, controls stack full-width.
- **Reduced motion**: honoured (no running animations/transitions).
- No colour-only state (labels + glyphs); visible focus outlines.

## 16. Local demonstration, CI, performance

### Local demonstration

```bash
cd frontend
npm install
npm run build
export AP_AGENT_TEST_POSTGRES_DSN=...   # must name the ap_agent_m8_test database; never printed
npm run demo:m11c                       # prints a http://127.0.0.1:<port>/dashboard URL
# explore, then Ctrl+C
npm run demo:m11c -- --check            # smoke test: start, verify the workspace renders, stop
```

The launcher fails closed on a missing DSN or any database other than
`ap_agent_m8_test`; seeds a fresh tenant (three named fixtures + four generic
cases); starts FastAPI write-enabled as a fresh least-privilege role and
Next.js in `commit` mode as `AP_REVIEWER`; prints only the URL and safe status
lines; and on exit stops both servers and deletes the tenant's removable rows.
`--check` was executed and left no process behind.

**Append-only residue (by schema design).** Decisions, audit events, invoice
memory, workflows, tenants and any review case that has a decision remain
after cleanup: `review_decisions` references `review_cases`
(`decision_review_fk`) and refuses mutation. Only decision-free cases are
deleted. Decided cases are `RESOLVED`/`REJECTED` and never appear in the
active queue.

### CI

`.github/workflows/m11a-frontend.yml` was **extended in place** (no duplicate
workflow): backend triggers widened; targeted M10/M11C command-policy,
capability and error-mapping unit tests; the M11C mocked Playwright suite
(reusing the production build via `AP_AGENT_SKIP_BUILD=1`); in the
`real-integration` job the M10/M11C PostgreSQL tests (via the existing
`tests/api/` step) and `npm run test:e2e:real-actions`, which enables FastAPI
writes **only inside its own child process** for the isolated tenant. No
write-enabled default is committed; the DSN is never printed; npm/pip/
Playwright caches preserved; PaddleOCR is not run. Do not push while a run is
in progress and avoid self-referential hash edits.

### Performance (real stack, local)

| Measure | Value |
|---|---|
| Client JS for the action workspace | one chunk, 43 KB raw / **11.9 KB gzip** (whole app 659 KB raw / 201 KB gzip) |
| Detail page load (real DB) | ~450 ms (two runs: 448–450) |
| Command submit → result | 142–170 ms |
| Result → refreshed authoritative state | 90–96 ms |
| Duplicate requests | none: exactly one `POST` per user action |
| Console errors / hydration warnings | none in the real-stack runs |

Only `ReviewActions*` are Client Components; the eight read-only detail
sections remain Server Components.

## 17. Visual walkthrough

Screenshots were captured from the real stack (real Chromium, FastAPI,
PostgreSQL) into `docs/m11c-screenshots/`. Dashboard → queue screenshots are in
the M11A/M11B reports; the M11C additions follow.

| Step | Screenshot | What happens / database state |
|---|---|---|
| Unclaimed case | `01-unclaimed-case.png` | Status Open, Unassigned, only **Claim** offered. Database unchanged |
| Claim | `02-claimed-case.png` | `review_cases`: `CLAIMED`, `assigned_to` = reviewer; workflow revision 1→2; one `INTERFACE_COMMAND_EXECUTED` audit event; the queue and dashboard "unassigned" count follow |
| Correction editor | `03-correction-editor.png` | Original value shown read-only; nothing has been written |
| Evidence selection | `04-evidence-selection.png` | Only this case's evidence references are selectable |
| Approve confirmation | `05-approve-confirmation.png` | Explicit, append-only warning; nothing executed until confirmed |
| Reject confirmation | `06-reject-confirmation.png` | States the decision is terminal for the case |
| Validation-only | `07-validation-only-result.png` | "Validated — no database changes were made." Full-tenant DB fingerprint unchanged |
| Stale conflict | `08-stale-conflict.png` | 409 copy, refresh required, form kept; DB shows only the winner's claim |
| Decision history | `09-resolved-decision-history.png` | Append-only `review_decisions` row (approved/corrected/rejected) with reasons, notes and reviewer; the **original** normalized memory and its hash are unchanged — corrections live in the decision's `evidence` (derived), not in the invoice record |
| Resume-eligible | `10-resume-eligible.png` | Resolved, approved/corrected, deciding reviewer, resume not yet requested |
| Resume handoff | `11-resume-handoff-result.png` | Audit event `WORKFLOW_RESUME_REQUESTED` (restart stage + derived version + plan id), workflow moved to the restart stage, revision +1. **A handoff exists; the pipeline has not run** |
| Mobile | `12-mobile-action-workspace.png` | 390 px layout |

What has **not** executed at the end of the walkthrough: no OCR, no financial
re-validation, no matching, no memory persistence, no payment, no ERP — the
restart stage is recorded for M11D to consume.

## 18. Known limitations

- After a page reload the restart stage/derived version of an *earlier* resume
  are not re-displayed (the panel says a handoff already exists); the values
  are in the audit event and were shown in the immediate result.
- Command identity is in memory; a reload mid-flight starts a new operation
  (safe via revision checks).
- Reason-code presets are UI vocabulary; the backend only requires a non-blank
  code.
- The remote Neon database was not reachable from the build sandbox; its
  coverage comes from CI on the PR.
- Prototype header authentication; no public write-enabled deployment.

## 19. Merge-readiness and M11D

Merge readiness is conditional on the PR's CI (including the write-enabled
real-PostgreSQL job against the remote `ap_agent_m8_test`) being green on the
latest head and no unresolved review threads. All locally executable suites
pass (§13). **M11D** (consuming the resume handoff to actually resume the
pipeline outside the HTTP request) can start once merged: the handoff plan,
restart stage, derived version, decision id and the append-only audit trail it
needs are all persisted and verified.

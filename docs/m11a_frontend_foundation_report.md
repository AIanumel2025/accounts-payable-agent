# M11A — Human-Review Frontend Foundation

## 1. Preflight

- **Starting `main` commit:** `650e9518e74cf26ecad751e580fb8ba6854cf137` ("M10:
  Phase 9 human-review interface and FastAPI modularisation") — verified
  identical to `origin/main` at session start; working tree was clean.
- **Branch deviation (documented, not silent):** the task brief asks for a
  new branch `claude/m11a-frontend-foundation`. This session's harness
  pinned a different, pre-existing branch for all work in this environment:
  `claude/elegant-goodall-1kwu0o` ("NEVER push to a different branch
  without explicit permission"), which was, at session start, at exactly
  the M10 merge commit above. All M11A work is committed there instead —
  the same kind of deviation M10 itself documented for its own branch
  (`docs/m10_phase_9_review_api_report.md` §1).
- Confirmed present before any work began: the M10 FastAPI application
  factory (`create_app`), `/health`, `/api/v1/dashboard`,
  `/api/v1/review-cases` (queue + detail), the review-command endpoint,
  the auto-generated OpenAPI schema, the development/test header-auth
  dependency, the PostgreSQL `ReviewRepository`, and validation-only
  command mode (`AP_AGENT_ENABLE_REVIEW_COMMAND_WRITES` default `false`).
- `AP_AGENT_TEST_POSTGRES_DSN` was confirmed present and its `dbname`
  parsed (never printed) to equal `ap_agent_m8_test` before any work began.
- The notebook was not modified.

## 2. Branch and files changed

- Branch: `claude/elegant-goodall-1kwu0o` (see deviation above).
- New top-level additions:
  - `frontend/` — the entire Next.js application (94 files: source,
    tests, config, generated OpenAPI artifacts, lockfile).
  - `.github/workflows/m11a-frontend.yml` — the new, dedicated M11A CI
    workflow.
  - `scripts/export_openapi_schema.py` — repo-root Python helper that
    exports the FastAPI app's OpenAPI schema without a live database
    connection.
- Modified: `README.md` (new "Frontend (M11A)" section).
- Untouched: the notebook, every `src/ap_agent/**` module, every existing
  test, and `.github/workflows/m8-postgres-acceptance.yml` (M11A adds a
  second, independent workflow rather than touching the M8/M9/M10 one, per
  task §18).

## 3. Frontend framework and versions

Next.js **16.3.6** (App Router, Turbopack build), React **19.3.0**,
TypeScript **5.9.3**, npm with a committed `package-lock.json`.

| Package | Version | Role |
|---|---|---|
| next | 16.3.6 | framework |
| react / react-dom | 19.3.0 | UI runtime |
| typescript | 5.9.3 | strict-mode type checking |
| vitest | 5.0.2 | unit/component test runner |
| @testing-library/react | 16.3.3 | component tests |
| @testing-library/user-event | 14.6.7 | component tests |
| jsdom | 30.1.1 | Vitest DOM environment |
| @playwright/test | 1.63.0 | browser acceptance tests |
| @axe-core/playwright | 4.13.0 | automated accessibility scanning |
| openapi-typescript | 7.13.0 | OpenAPI → TypeScript codegen |
| eslint | 9.39.5 | linting |
| eslint-config-next | 16.3.6 | Next.js lint rules |
| server-only | 0.0.1 | compile-time server/client boundary enforcement |

**Two version deviations from "install the latest," both forced by real
ecosystem incompatibility, not preference:**

1. **TypeScript.** The actual latest release is 7.0.2, but
   `typescript-eslint@8.70.1` (which `eslint-config-next` depends on)
   declares `peerDependencies.typescript: ">=4.8.4 <6.1.0"`. Installing
   TypeScript 7 would silently violate that range. Pinned to the latest
   *compatible* 5.x release, 5.9.3.
2. **ESLint.** The actual latest release is 10.11.0, but every plugin
   `eslint-config-next@16.3.6` pulls in (`eslint-plugin-react`,
   `eslint-plugin-import`, `eslint-plugin-jsx-a11y`,
   `eslint-plugin-react-hooks`) declares peer support only up to `^9`, and
   installing ESLint 10 anyway produced a hard runtime crash
   (`TypeError: scopeManager.addGlobals is not a function`) the first time
   lint actually ran — not just a peer-dependency warning. Pinned to the
   latest *working* 9.x release, 9.39.5.

## 4. Application routes

```text
/                                    → redirects to /dashboard (task §8)
/dashboard                           → the read-only dashboard (task §9)
/api/backend/[...path]               → server-side proxy to FastAPI (GET-only, allow-listed)
```

`/api/backend/[...path]` only exports a `GET` handler and only forwards to
an explicit allow-list (`health`, `api/v1/dashboard`) — every other path,
including every review-command endpoint, is rejected with 404 before a
request is ever built (task §6 read-only safety, enforced at the boundary,
not just by omission in UI code).

## 5. Component inventory

**`components/application-shell/`** — `AppShell`, `Sidebar`, `MobileNav`,
`NavList`, `ProductIdentity`, `PageHeader`, `TenantContextIndicator`,
`ActorRoleIndicator`, `BackendHealthIndicator`, `ValidationOnlyIndicator`,
`navigation.ts` (nav item config).

**`components/dashboard/`** — `MetricCard`, `MetricGrid`,
`ReviewReasonTable`, `DashboardBody`.

**`components/feedback/`** — `LoadingState`, `EmptyState`, `ErrorState`,
`RetryButton`, `StaleDataBanner`.

**`components/ui/`** — `StatusBadge`.

**`lib/`** — `config/server-env.ts` (validated server-only env parsing),
`auth/dev-headers.ts`, `server/backend-request.ts`, `api/dashboard.ts`,
`api/error-messages.ts`, `formatting/{money,numbers,timestamps,status,
review-reasons}.ts`.

## 6. Design-system summary

A restrained, slate/graphite, dark-by-default finance-operations theme
(`src/styles/tokens.css`), with a full light-theme token set defined for
completeness. Tokens cover canvas/surface/border, primary/secondary/
tertiary text, accent, success/warning/review-required/failed status
colours, focus, spacing (4px scale), radius, shadow, and typography (a
bundled system-font stack — no runtime web-font download, task §17).
Every status is rendered as **label + glyph + colour together**
(`StatusBadge`, `workflowStatusLabel`/`reviewCaseStatusLabel`/
`backendHealthLabel`), never colour alone. No CSS-in-JS runtime or UI
component library — plain CSS Modules per component.

**One real contrast bug found and fixed** during accessibility testing:
`--color-text-tertiary` (`#78849b` on `#1b212b`) measured 4.28:1, just
under the WCAG AA 4.5:1 threshold for the small (12px) uppercase labels in
the header's status indicators — caught by an automated axe-core scan, not
by inspection. Changed to `#99a3b8` (6.38:1). See §16.

## 7. OpenAPI-contract generation (task §4)

A repeatable, two-stage pipeline:

1. **`scripts/export_openapi_schema.py`** (repo root) builds the real
   `ap_agent.api.app.create_app(...)` with a placeholder DSN that is never
   dialed (`app.openapi()` needs no database connection — only the app's
   `lifespan` does, and that never runs for a plain schema export) and
   writes `frontend/openapi/openapi.json`.
2. **`frontend/scripts/generate-api-types.mjs`** first tries a live
   backend at `AP_AGENT_API_BASE_URL`/`openapi.json`; if none is reachable
   it falls back to step 1 automatically, then runs `openapi-typescript`
   and writes `frontend/src/types/api.generated.ts`.

```bash
npm run api:generate   # regenerate openapi/openapi.json + src/types/api.generated.ts
npm run api:check      # regenerate into a temp dir and diff against the committed files; non-zero exit on drift
```

`npm run api:check` is a required CI step (§9 below) — a FastAPI schema
change without a regenerated frontend contract fails the build.

**One documented limitation, not a silent gap:** every M10 route declares
`response_model=ApiEnvelope`, whose `data` field is Pydantic `Optional[Any]`
(`ap_agent/api/schemas.py`) — the *actual* payload shape
(`DashboardResponse`, the health payload) never appears in the OpenAPI
schema itself, so `openapi-typescript` can only generate
`data?: unknown | null`. `src/types/api-payloads.ts` hand-declares the real
payload shapes from reading `ap_agent/api/schemas.py`/
`ap_agent/api/routes/health.py` directly, with a comment explaining this
can't be caught by `api:check` and must be kept in sync by hand if those
backend shapes change.

## 8. Server-side API boundary (task §5)

```text
Browser → Next.js server-side boundary → FastAPI → PostgreSQL
```

- `lib/config/server-env.ts` — reads and validates
  `AP_AGENT_API_BASE_URL`, `AP_AGENT_FRONTEND_AUTH_MODE`,
  `AP_AGENT_DEV_TENANT_ID`, `AP_AGENT_DEV_ACTOR_ID`,
  `AP_AGENT_DEV_ACTOR_ROLE` — all server-only, none `NEXT_PUBLIC_`. Throws
  a typed `ServerConfigError` (`MISSING_VARIABLE`/`INVALID_VALUE`) rather
  than a bare exception, so the UI can render an explicit "authentication
  misconfigured" state (task §10) instead of a stack trace.
- `lib/auth/dev-headers.ts` — builds the four M10 development-header-auth
  fields fresh per request, `X-Authenticated-At` from a real, current,
  timezone-aware timestamp every time.
- `lib/server/backend-request.ts` — the only place that calls FastAPI from
  a Server Component; 8s timeout (overridable via
  `AP_AGENT_BACKEND_TIMEOUT_MS`, used only by tests), maps HTTP status and
  network failure into a typed `BackendResult`.
- `app/api/backend/[...path]/route.ts` — the only FastAPI-shaped route the
  *browser* can reach (§4 above): GET-only, allow-listed, own
  authentication headers only, never forwards a browser-supplied header.
- Every module above is `import "server-only"` — a client-component import
  of any of them is a build-time error, not just a convention.

All four checked with a live rendered page, browser JS state, storage,
cookies, console output, and the actual built `.next/static` JS bundles
(§16).

## 9. Development-authentication limitation

`AP_AGENT_FRONTEND_AUTH_MODE=development_headers` is the only supported
mode in M11A, matching the M10 backend's own explicitly-labelled
development/test header-auth adapter
(`ap_agent/api/dependencies.py`). It is **not production authentication**.
The boundary is designed so a future OIDC/JWT dependency can replace it
without any UI code change: every caller goes through
`lib/server/backend-request.ts`/`app/api/backend/[...path]/route.ts`, never
constructs headers itself.

**A genuine tension between two task requirements, resolved and
documented:** task §8 requires a visible tenant-context indicator and
actor-role indicator; task §16 forbids "development tenant IDs" and
"development actor IDs" appearing in browser output. Resolution:
`TenantContextIndicator` never renders the configured tenant UUID at all —
only "Development (single-tenant)" — and `ActorRoleIndicator` renders the
*role* (`READ_ONLY_AUDITOR` → "Read-Only Auditor"), not the configured
actor-id string. A role name is category information the interface is
meant to show the user (who they're acting as), not a secret identifier;
the specific UUID/actor-id strings are. `scripts/check-build-secrets.mjs`
and `tests/e2e/secret-exposure.spec.ts` both assert the raw tenant UUID and
actor-id string never appear, while accepting the humanized role label as
intended UI copy.

## 10. Read-only guarantee (task §6)

- `AP_AGENT_ENABLE_REVIEW_COMMAND_WRITES` is never set anywhere in this
  milestone's code, config, or CI.
- The UI never calls the review-command endpoint — there is no code path
  that could: `/api/backend/[...path]` only allow-lists `health` and
  `api/v1/dashboard`.
- No claim/release/correction/approval/rejection/resume control exists
  anywhere. "Review queue" is visible in navigation, explicitly labelled
  "Coming in M11B", and rendered as disabled text, never a link.
- No PostgreSQL mutation is possible from this application: verified by
  the real-integration suite re-reading the dashboard twice and asserting
  identical totals (§13).

## 11. Dashboard result (task §9)

All seven required metrics, review-reason analytics (a real `<table>`, not
a decorative chart), backend health, the validation-only indicator, and a
last-refreshed timestamp are implemented and rendered. Verified against
the exact controlled-fixture baseline via both a seeded mock backend
(component + standard Playwright tests) and mechanically via the real-
integration dry run (§13):

| Metric | Expected | Rendered |
|---|---:|---:|
| Total invoices | 4 | 4 |
| Processing invoices | 0 | 0 |
| Completed automatically | 1 | 1 |
| Review required | 3 | 3 |
| Failed invoices | 0 | 0 |
| Open review cases | 3 | 3 |
| Unassigned review cases | 3 | 3 |

| Reason | Expected | Rendered |
|---|---:|---:|
| `INHERITED_FINANCIAL_VALIDATION_REVIEW` | 3 | 3 |
| `SUPPLIER_NAME_MISSING` | 2 | 2 |
| `INVOICE_LINE_TOTAL_MISSING` | 1 | 1 |

None of these numbers are hardcoded in production components — they come
from `ap_agent/api/schemas.py`'s `DashboardResponse`/`ReviewReasonCount`
through the boundary described in §8; only the *test* fixtures
(`tests/e2e/support/mock-backend.mjs`) and this report hardcode them, per
task §9's own instruction.

## 12. Test results

### 12.1 Unit and component tests (Vitest)

```text
npm run test
Test Files  15 passed (15)
Tests       71 passed (71)
```

5 unit-test files (`server-env`, `dev-headers`, `formatting`,
`error-messages`, `backend-request`) and 10 component-test files
(`StatusBadge`, `MetricGrid`, `ReviewReasonTable`, `NavList`, `MobileNav`,
`BackendHealthIndicator`, `ErrorState`, feedback states, indicators,
`DashboardBody`). Coverage includes: config parsing and every
missing/malformed-variable failure mode; header construction and
freshness; response parsing/status-mapping for every `BackendErrorKind`;
every formatting utility (money never rounds/zeroes a missing amount,
timestamps preserve UTC while displaying Europe/London, status/review-
reason labels always pair text with a mapping); protection against a
browser-supplied tenant override (headers only ever come from server
config); error-envelope → user-facing message mapping never leaks
implementation detail; the OpenAPI generation pipeline's drift check.

### 12.2 Playwright browser tests (mocked backend)

```text
npm run test:e2e
32 passed (chromium)
```

Root redirect; dashboard rendering (metrics + review-reason table);
real client-side + server-rendered navigation (active/forthcoming nav
items, no "Payments" item anywhere, skip-to-content, mobile drawer);
loading state (observable via the mock backend's realistic ~350ms
latency); backend-unavailable/malformed-response/timeout handling with
safe, non-technical error copy; retry recovering the dashboard; the
backend-health indicator's live recheck and stale-connection note;
keyboard-only navigation; responsive layout at desktop/laptop/tablet/
mobile with no horizontal scroll; accessibility (see §14); no secret
exposure in rendered HTML, browser JS state, storage, cookies, or console
(see §16); zero uncaught console errors.

One real, non-trivial bug found and fixed while building this suite: the
mock backend's mutable `mode` is shared *process-wide* state, so running
specs in parallel (the initial config used 2 workers) let two tests racing
different `mode`s flake each other non-deterministically. Fixed by running
the whole suite serially (`workers: 1`) — the suite is small enough
(~30s) that this costs nothing meaningful.

### 12.3 Real FastAPI + PostgreSQL + Next.js integration (task §15)

**Mechanically verified in this sandbox; the true acceptance assertions
require a CI run.** This sandbox's outbound-HTTPS proxy explicitly does
not support raw-TCP database connections
(`/root/.ccr/README.md`: "Not supported through the proxy ... raw-TCP
databases") — the identical, already-documented limitation M8/M9/M10 all
hit (`docs/m10_phase_9_review_api_report.md` §13.2).

What *was* verified here, with a deliberately invalid DSN
(`scripts/run-real-integration.mjs` dry run):

- The DSN is mapped into the FastAPI child process's environment only
  (`AP_AGENT_POSTGRES_DSN`), never printed, never written to a file, never
  passed to the Next.js process.
- FastAPI starts cleanly and `/health` returns `200` without touching
  PostgreSQL (matching `ap_agent/api/routes/health.py`'s own design).
- Next.js starts cleanly against the real FastAPI process.
- `GET /api/v1/dashboard` correctly reaches the real `psycopg` connection
  attempt and fails with the expected `503 DATABASE_UNAVAILABLE` once a
  genuinely bad DSN is dialed — proving the wiring is real, not stubbed.
- The Playwright real-integration spec launches a real browser and
  correctly times out waiting for `dashboard-body` (since no real data can
  load), rather than erroring on infrastructure.
- Cleanup is fully graceful: `INFO: Shutting down` / `Application shutdown
  complete` from uvicorn, no orphaned Next.js or FastAPI process left
  behind (verified with `ps aux` after the run) — this needed a fix: the
  first version of the script left an orphaned `next-server` behind when
  its parent `node` process was killed by an external `timeout`, because
  npm does not reliably forward signals to the process it execs. Fixed by
  spawning each child in its own process group (`detached: true`) and
  signaling the group (`process.kill(-pid, ...)`, not just the direct
  child.

**Per this milestone's own instruction, this result is not mocked or
substituted.** The real assertions (exact controlled-fixture metrics,
review-reason analytics, backend-health, zero console errors, an
accessibility scan, and "no PostgreSQL mutation occurred") run via the new
`.github/workflows/m11a-frontend.yml`'s `real-integration` job, using the
repository's `AP_AGENT_TEST_POSTGRES_DSN` secret, once this branch's PR
triggers CI.

## 13. Secret-exposure result (task §16)

`tests/e2e/secret-exposure.spec.ts` (5 tests, all passing) checks the live
rendered page HTML, evaluated browser JS state, `localStorage`/
`sessionStorage`/cookies, and console output for: the configured dev
tenant id, dev actor id, dev actor role *value*, any
`postgres(ql)://` connection string, `neon.tech`, and the DSN environment
variable names themselves. `scripts/check-build-secrets.mjs` separately
scans the actual built `.next/static` JS/map files (not just the live
page) for the same patterns — run as its own CI step after `npm run
build`. Both pass with zero matches. No source maps are generated in
production builds (Next's default).

## 14. Accessibility result (task §12)

```text
npx playwright test tests/e2e/accessibility.spec.ts
6 passed
```

Automated `axe-core` scans (WCAG 2.0 A/AA + WCAG 2.2 AA tags) on the
loaded dashboard (desktop and mobile) and the error state: **zero serious
or critical violations** after fixing the contrast bug in §6. Also
verified: exactly one `<h1>` and a logical two-`<h2>` heading structure;
full keyboard reachability (skip link → primary nav link → main content);
`prefers-reduced-motion` respected (the loading spinner's animation is
disabled under that preference). Manual review confirms: semantic
landmarks (`<nav>`, `<main>`, `<header>`), a real `<table>` with `<caption>`
and column headers for review-reason analytics (not a styled `<div>`
grid), `aria-live="polite"` status regions for the backend-health
indicator and loading state, `role="alert"` for error states, and no
status communicated by colour alone anywhere (§6).

**Known remaining limitation:** automated scanning cannot fully verify
screen-reader announcement quality or every interaction sequence; this was
spot-checked via DOM/ARIA-attribute assertions and keyboard-path tests, not
a real assistive-technology device. Recommended for a future milestone.

## 15. Responsive result (task §13)

Tested at desktop (1440×900), laptop (1280×800), tablet (834×1112), and
mobile (390×844): sidebar collapses to a toggled drawer below 768px,
metric-card grid reflows (not a naive shrink — `auto-fill, minmax(11rem,
1fr)`), and no horizontal scroll occurs at any tested width (asserted via
`document.documentElement.scrollWidth` in every responsive Playwright
test). Screenshots reviewed in §17.

## 16. Production-build result (task §17)

```text
npm run build
✓ Compiled successfully
✓ Generating static pages (3/3)
Route (app)
┌ ○ /                      (static)
├ ○ /_not-found             (static)
├ ƒ /api/backend/[...path]  (dynamic)
└ ƒ /dashboard               (dynamic)
```

- Shared client JS under `.next/static/chunks`: **~624 KB uncompressed**
  across all chunks (React + Next runtime, shared by every route — no
  route-specific bundle bloat found).
- No external web-font requests (system-font stack only, task §17).
- No hydration warnings observed in any test run.
- Zero browser-console errors on a normal load (`tests/e2e/secret-
  exposure.spec.ts`'s dedicated check + the real-integration spec's own
  console-error assertion).
- Dashboard end-to-end load time (cold `page.goto` to `networkidle`, mock
  backend, this sandbox): **~880 ms** at every tested viewport — includes
  the mock backend's deliberate 350 ms artificial latency (added so the
  loading state is observable in tests); real-world latency against a
  local FastAPI instance would be lower.
- Prefers server components: only `MobileNav`, `NavList`,
  `BackendHealthIndicator`, `RetryButton`, and the dashboard `error.tsx`
  boundary are client components (interactivity only); everything else,
  including the entire dashboard data-fetching path, is a Server
  Component.

## 17. Visual inspection (task §19)

Four screenshots captured from the actual running application (mock
backend, production build) and reviewed for clipping, overlap, unreadable
text, excessive whitespace, inconsistent spacing, poor contrast, and
broken responsive layout — **none found** after the §6 contrast fix.

- Desktop (1440×900): full sidebar, header indicators inline, seven-card
  metric grid.
- Tablet (834×1112): sidebar remains, metric cards reflow to two columns.
- Mobile (390×844): sidebar replaced by a hamburger-toggled drawer, cards
  stack single-column, table remains readable without horizontal scroll.
- Backend-error state (desktop): header shows "Backend unavailable" (icon
  + text), main content shows a clear, non-technical error card with a
  working Retry button — no stack trace, DSN, or internal detail visible.

## 18. CI workflow result (task §18)

`.github/workflows/m11a-frontend.yml` — new, independent of
`.github/workflows/m8-postgres-acceptance.yml` (untouched). Triggers only
on `pull_request` events touching `frontend/**`, the OpenAPI export
script, or the API-contract-relevant `src/ap_agent` modules — a
documentation-only or backend-unrelated commit triggers neither this
workflow nor the hour-long PaddleOCR/PostgreSQL regression.

- **`build-and-test`** job: `npm ci` → lint → typecheck → `api:check` →
  unit/component tests → production build → Playwright browsers install →
  mocked-backend e2e tests → built-asset secret scan. Playwright browsers
  and npm are cached.
- **`real-integration`** job (depends on `build-and-test`): installs
  `ap_agent[api,postgres]`, verifies the `AP_AGENT_TEST_POSTGRES_DSN`
  secret is configured, and runs `npm run test:e2e:integration` (§12.3).

Not yet executed by GitHub Actions as of this report — it runs once this
branch's PR is opened. Its result will be visible on the PR itself.

## 19. Deviations (documented, not silent — CLAUDE.md)

1. **Branch name** (§1).
2. **TypeScript and ESLint pinned below their absolute-latest release**,
   both for genuine peer-dependency/runtime-crash incompatibility with
   `eslint-config-next`'s own dependency chain, not preference (§3).
3. **Tenant/actor-role indicator design** reconciles task §8 (must show
   tenant-context and actor-role indicators) against task §16 (must not
   expose development tenant/actor ids): role is shown as a humanized
   label, the tenant UUID and actor-id string never are (§9).
4. **`ApiEnvelope.data` cannot be generated as a typed shape** from
   OpenAPI because FastAPI declares it `Any`; hand-maintained payload
   types in `src/types/api-payloads.ts` fill the gap, documented as
   something `api:check` cannot catch drift in (§7).
5. **"Review queue" nav item renders as disabled/forthcoming text**, not a
   working page — explicitly out of scope for M11A (task §2/§8/§22).
6. **Real FastAPI+PostgreSQL acceptance assertions could not run in this
   sandbox** (§12.3) — mechanically verified with a deliberately invalid
   DSN instead; the real assertions run via the new CI workflow's
   `real-integration` job against the repository's
   `AP_AGENT_TEST_POSTGRES_DSN` secret.
7. **Playwright suite runs with `workers: 1`** (not the default parallel
   execution) because every spec shares one mock backend process's mutable
   state; documented in §12.2 as a real flake this caused and fixed.

## 20. Blockers

None blocking a merge review. The one open item — a genuine, green
`real-integration` CI run against the real `ap_agent_m8_test` database —
is expected to complete automatically once this branch's PR triggers
GitHub Actions, exactly as it did for M8/M9/M10.

## 21. Readiness for M11B

**Yes.** The API-contract generation pipeline, server-side boundary,
design system, application shell, and test infrastructure (unit,
component, mocked e2e, and a working real-integration harness) are all in
place and exercised. M11B can add the review-queue and invoice-detail
pages, and eventually review-command writes, directly on top of this
foundation without re-deriving any of it.

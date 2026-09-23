# accounts-payable-agent

Modular extraction of a validated Jupyter notebook (`notebooks/accounts_payable_pipeline.ipynb`,
Phases 1-5) into an installable package at `src/ap_agent/`. The full inventory,
cell-by-cell provenance and migration plan live in
`docs/modularisation_map.md` — read it before extracting anything new.
Milestone reports (`docs/m<N>_*.md`) record what each milestone actually did.

## Non-negotiable rules

- **The notebook is the behavioural reference and is never modified.** It is
  read-only source of truth for every extraction.
- **Verbatim extraction only.** Copy the *active, validated* definition for
  each phase's entry point — names, field order, defaults, branch order,
  message strings, reason codes, rounding, JSON layouts. Do not "clean up"
  or refactor while extracting. Do not extract the textually last
  definition of a repeated name without checking which one the validated
  run actually used (`docs/modularisation_map.md` §3.2).
- **Fixtures never enter production code.** The four invoices in
  `tests/fixtures/invoices/` (manifest: `tests/fixtures/invoices/manifest.json`)
  are controlled regression fixtures. Runtime code under `src/` must never
  hardcode their filenames, expected totals, OCR anchors, or assume there
  are exactly four (or seven) documents (decisions D-2, D-3).
- **Configuration is threaded explicitly, never read as a hidden global.**
  Every `*Config` dataclass/model is passed into functions as an argument.
  No module under `src/` may define a mutable module-level config
  *instance* (`docs/modularisation_map.md` §5.1).
- **Two intentionally different behaviours stay separate under distinct
  names**, they are not "the last one wins": the Phase 2/3 vs. Phase 4 byte
  formats of `write_json_atomically`, and the Phase 4 vs. Phase 5
  `append_unique_reason` (falsy-reason handling differs). See §3.2 and
  decisions D-8/D-9.
- **No heavy or optional dependency is imported at module import time.**
  `pandas`, `matplotlib`, `IPython`, `paddle`, `paddleocr`, `pytesseract`,
  `pymupdf`, `cv2`, `numpy`, `PIL` must only be imported lazily, inside the
  functions/adapters that need them (never as a side effect of
  `import ap_agent` or importing any contract module). Package `__init__.py`
  files stay empty or lazy (`__getattr__`) — no eager re-exports (C-5).
- **Dependency direction is one-way:** `orchestration → tools → adapters →
  artifacts → models`; `config → models`; `exceptions → models.ingestion`.
  `models/` never imports `config/` or `exceptions`. See §4.3/§4.4 for the
  full acyclic graph and how to avoid the listed cycle risks (C-1..C-8).
- **Known, deliberate deviations from the notebook must be documented, not
  silently fixed**, unless a milestone explicitly schedules and tests the
  behaviour change (e.g. the stale Phase 3 OCR artifact status, R-04/D-4/D-5).
  Record every new deviation in that milestone's report.
- **Record deviations, don't hide them.** If a ported self-test fails
  against the active (non-superseded) implementation, that is a finding to
  report, not a reason to quietly patch production to match an outdated
  assertion (R-08).

## Repository layout

- `src/ap_agent/` — the installable package (src layout).
- `tests/unit/`, `tests/integration/`, `tests/golden/`,
  `tests/fixtures/invoices/` — tests and controlled fixtures.
- `notebooks/` — the read-only behavioural reference.
- `docs/modularisation_map.md` — full M1 inventory and migration plan.
- `docs/m<N>_*.md` — per-milestone extraction reports.

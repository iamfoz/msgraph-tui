# Graphdeck — Architecture

## 1. Technology choice

Four stacks were evaluated against the MVP priorities (fast iteration, rich TUI widgets, tables, dialogs/forms, responsive async, reliable subprocess management for PowerShell, robust HTTP for Graph, maintainability, testability, security, packaging):

| Criterion | Python + Textual | .NET + Spectre.Console | Go + Bubble Tea | Rust + Ratatui |
|---|---|---|---|---|
| Widget richness (tables, trees, dialogs, forms, tabs, palette) | **Excellent** — DataTable, Tree, ModalScreen, built-in command palette, CSS theming | Good rendering, but Spectre is primarily *output*-oriented; interactive full-screen apps need significant custom work | Moderate — Bubbles provides primitives; complex forms/dialogs are DIY | Low-level — everything is DIY; highest effort per widget |
| Async model for concurrent Graph calls + UI | **Excellent** — asyncio-native app, `@work` workers, message pump | Good (async/await) but TUI event loop is custom | Good (goroutines + Elm architecture) | Good but most ceremony |
| HTTP/OAuth ecosystem | **httpx + MSAL (first-party Microsoft library)** | Excellent (Microsoft.Identity) | Good | Good |
| PowerShell subprocess management | Excellent (`asyncio.subprocess`, no shell) | Excellent (PowerShell SDK hosting — best-in-class) | Good | Good |
| Iteration speed for a large screen surface | **Fastest** | Medium | Medium | Slowest |
| Testability without a terminal | **Excellent** — Textual `run_test` pilot + pure-Python core | Medium | Good | Good |
| Type safety | Good (typing + dataclasses, mypy-able) | Excellent | Good | Excellent |
| Packaging | Good (pipx/uv; PyInstaller if needed) | Good | **Excellent** (single binary) | Excellent |

**Decision: Python 3.11+ with Textual.** The dominant cost in this product is *screen surface* (tables, forms, wizards, diff views, modals) and *provider plumbing*, not raw performance. Textual is the only evaluated framework with production-grade versions of every widget the PRD requires, a CSS-like theming system, a built-in command palette, and a first-class async test harness — which directly serves the "tests without a tenant" requirement. Microsoft ships MSAL for Python (first-party auth), and Graph is plain REST over httpx. The strongest counter-argument — .NET's in-process PowerShell hosting — matters less because we deliberately run PowerShell out-of-process (isolation, redaction at the boundary, cross-platform pwsh support), and Go/Rust's single-binary packaging is a distribution nicety, not an MVP requirement.

Trade-offs accepted: Python runtime prerequisite (mitigated by `pipx`/`uv` install docs); GIL irrelevance (I/O-bound workload).

## 2. System overview

```
┌────────────────────────────── Textual App (app/, ui only) ─────────────────────────────┐
│ Dashboard │ Nav tree │ Browse views │ Detail │ Preview modal │ Audit/Changes │ Logs    │
└─────┬──────────────────────────────────────────────────────────────────────────────────┘
      │  (async calls; UI never talks to providers directly)
┌─────▼──────────────── services/ ────────────────────┐
│ Executor (read path + write pipeline)               │
│  plan → preview → before-state → snapshot → reason  │
│  → confirm → execute → after-state → audit → verify │
└─────┬───────────────┬──────────────┬────────────────┘
      │               │              │
┌─────▼─────┐   ┌─────▼─────┐  ┌─────▼──────┐
│ core/     │   │compliance/│  │ providers/ │
│ registry  │   │ audit     │  │ graph_rest │──► Microsoft Graph v1.0/beta (httpx+MSAL)
│ selection │   │ (hash     │  │ graph_sdk  │──► (stub; post-MVP)
│ envelope  │   │  chain)   │  │ powershell │──► pwsh / powershell.exe subprocess
│ redaction │   │ rollback  │  │ mock       │──► fixtures/*.json (in-memory tenant)
│ errors    │   │ evidence  │  └────────────┘
│ config    │   └───────────┘
│ export    │
└───────────┘
```

Hard rules:
- **UI layer is dumb**: it renders registry metadata and envelopes; it never builds requests or interprets provider errors.
- **Everything crossing an output boundary passes the redaction service** (previews, logs, audit, exports).
- **Engines are interchangeable** behind `Provider` (async `preview()` / `execute()` returning `ResultEnvelope`); the mock engine is a full peer, which is what makes the entire product testable offline.
- **All write behaviour is data**: risk, confirmation level, rollback spec, and audit requirement live in the action definition, enforced centrally by the Executor — a module author cannot forget them (registry validation rejects incomplete write actions).

## 3. Package layout

```
src/msgraph_tui/
├── core/            envelope, errors, redaction, actions (registry), providers (base),
│                    selection, config, export
├── providers/       graph_rest (auth + engine), powershell (builder + engine),
│                    graph_sdk (stub), mock (engine + state)
├── compliance/      audit (hash-chained JSONL), rollback (snapshots + sanity checks),
│                    evidence (pack export)
├── modules/         users, groups, licenses  → pure ActionDefinition declarations
├── services/        executor (read path + 13-step write pipeline)
├── app/             Textual application: main, views, modals, theme CSS
└── fixtures/        mock-tenant JSON
tests/               pure-Python unit tests + Textual pilot smoke test
docs/                PRD, this file, capability matrix, guides
```

## 4. Key contracts

- **`ActionDefinition`** (`core/actions.py`): full declarative action contract (see the action-registry contract in `developer-guide.md`), with `GraphTemplate`, `PowerShellTemplate`, `ParamSpec`, `RollbackSpec` (read-action reference, changed-field extractor, inverse-request builder). Registry validates on registration.
- **`ResultEnvelope`** (`core/envelope.py`): the single common result envelope returned by every engine.
- **`Provider`** (`core/providers.py`): `name`, `is_available()`, `supports(action)`, `preview(action, params)`, `execute(action, params)`.
- **Provider selection** (`core/selection.py`): mock-mode override → per-action user preference → per-service preference → action preferred → remaining supported; filters out unavailable engines and beta-requiring providers when beta is disabled; returns the choice *and the reasons*, which the UI displays.
- **`AuditLog`** (`compliance/audit.py`): append-only JSONL, `entry_hash = sha256(prev_hash + canonical_json(entry))`, `verify()` returns first divergence.
- **`RollbackService`** (`compliance/rollback.py`): snapshot store, `sanity_check()` (existence, type, drift vs before/after, blast radius, destructive flag), diff production, inverse-plan creation. Rollback executes through the same write pipeline as any change, linked to the original operation ID.

## 5. Engine notes

**Graph REST**: URL/query built from templates with strict param substitution (no f-string of user input into paths — segments are URL-quoted); `$select` composition; pagination follows `@odata.nextLink` with page/row caps; 429/503 honoured via `Retry-After` with capped exponential backoff (idempotent requests only); `ETag` captured into envelope/rollback metadata; `Authorization` header never logged; correlation via `client-request-id`/`request-id`.

**PowerShell**: commands are built as token lists, values bound via single-quote PS literals with `'` doubling (injection-safe, tested with hostile input); executed as `pwsh|powershell -NoProfile -NonInteractive -Command <script>` via `asyncio.subprocess` with **no shell**; output contract is `| ConvertTo-Json -Depth 5 -Compress`; the parser tolerates warning/noise lines before/after the JSON document; module presence detected via `Get-Module -ListAvailable`; missing modules produce guidance, never auto-install; `-WhatIf` passthrough for dry-run where supported.

**Mock**: an in-memory tenant hydrated from `fixtures/*.json`; read actions serve fixture data with filter/search semantics; write actions mutate state (so before/after capture, audit and rollback are demonstrable end-to-end offline); `_simulate` parameter forces `throttled`, `permission_denied`, `not_found`, `expired_session`, `malformed` responses for resilience testing.

## 6. Modes

`live` (real engines), `mock` (mock engine only, forced selection), `dry-run` (pipeline runs through preview/confirmation, records an intent audit event, never executes). Mode is global, chosen at launch or in config, and permanently visible in the status bar.

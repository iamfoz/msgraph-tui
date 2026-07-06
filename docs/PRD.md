# Product Requirements Document — Graphdeck

**A provider-transparent Microsoft 365 administration console for the terminal.**

| | |
|---|---|
| Status | v1.0 — approved for MVP implementation |
| Working title | **Graphdeck** |
| Repository | `msgraph-tui` |
| Date | 2026-07-06 |

---

## 1. Product name suggestions

| Candidate | Rationale |
|---|---|
| **Graphdeck** (chosen) | "Graph" (the primary provider family) + "deck" (a console/bridge you operate from). Short, brandable, honest about what it wraps. |
| TenantDeck | Emphasises multi-tenant context awareness. |
| m365ctl | Familiar `*ctl` convention, but implies CLI-first rather than TUI-first. |
| Bastion 365 | Emphasises safety/guardrails; risks sounding like a security product. |
| Ledger365 | Emphasises audit/evidence; undersells the admin surface. |

The working title throughout this document is **Graphdeck**. The Python package is `msgraph_tui`, the launcher command is `graphdeck`.

---

## 2. Problem statement

Microsoft 365 administration is fragmented across at least five surfaces: the Microsoft 365 admin center, Entra admin center, Exchange admin center, Teams/SharePoint/Intune/Purview portals, Microsoft Graph (REST, SDKs, Graph PowerShell), and workload-specific PowerShell modules (Exchange Online, Teams, SharePoint Online, Security & Compliance).

The web portals are incomplete and inconsistent: important options are hidden, advanced settings are missing entirely, bulk operations are painful, and behaviour changes without notice. Administrators are therefore routinely forced into raw PowerShell or raw Graph calls — which are powerful but unguarded: no confirmation semantics beyond `-WhatIf` (where implemented), no before-state capture, no change record, no rollback plan, easy cross-tenant mistakes, and audit evidence that must be reconstructed manually for ISO 27001 / change-management purposes.

There is no tool that combines **the full power of the supported Microsoft interfaces** with **the safety, discoverability, and auditability that professional change management requires**.

Graphdeck fills that gap: a keyboard-first terminal console that executes administration through the *best supported Microsoft interface for each task* (Graph REST, Graph SDK, Graph PowerShell, or workload PowerShell modules), always shows the administrator exactly what will run, captures before/after state, produces tamper-evident audit evidence, and offers controlled rollback where technically safe.

**Explicit stance:** Graphdeck does **not** hide Microsoft Graph, the SDKs, or PowerShell. It makes them safer, clearer, faster, more discoverable, and more auditable. Every action maps visibly to the provider operation being performed; the request/command preview is a core feature, not a debug view.

## 3. Target users

- **IT administrators / M365 tenant admins** in SMB-to-enterprise organisations, MSPs managing many tenants, and internal IT teams.
- **Security and identity engineers** who audit privileged roles, app consents, and credentials.
- **Compliance / ISMS owners** who need change evidence for ISO/IEC 27001, SOC 2, or internal audit.
- **Helpdesk engineers (tier 2/3)** who perform routine user/group/licence/mailbox changes and need guardrails.

### 3.1 User personas

**Priya — MSP escalation engineer.** Administers 40 tenants. Terrified of running a command in the wrong tenant. Needs: persistent tenant context display, command preview, fast keyboard navigation, exportable results for tickets.

**Marcus — enterprise identity engineer.** Expert in Graph; annoyed that the Entra portal hides half the settings he needs. Needs: raw request visibility, beta-endpoint gating he controls, provider choice, quick export of role/app-permission reports.

**Sofia — ISMS / compliance manager.** Not a hands-on admin. Needs: evidence packs, change reasons tied to ticket references, tamper-evident change history, proof that high-risk changes were confirmed and approved.

**Dan — tier-2 helpdesk.** Runs the same 15 operations daily. Occasionally breaks things with copy-pasted PowerShell. Needs: guided forms, safe defaults, explicit warnings, rollback when he gets it wrong.

### 3.2 Jobs to be done

- "When I must change production M365 configuration, I want to see exactly what will be executed and be able to reverse it, so I can change confidently."
- "When an auditor asks who changed what and why, I want to export evidence in minutes, not reconstruct it from Unified Audit Log fragments."
- "When the portal doesn't expose a setting, I want a supported programmatic path with guardrails, not a StackOverflow PowerShell snippet."
- "When I operate across tenants, I want it to be structurally hard to run a change in the wrong tenant."

## 4. Key use cases

1. List/search/filter users; open detail; export to CSV for a ticket.
2. Disable a leaver's account with reason + ticket reference; capture before-state; roll back if HR retracts.
3. Add/remove group members with a rollback snapshot of the membership delta.
4. Report subscribed SKUs and consumption; find disabled users still holding licences.
5. Inspect an app registration's permissions and credential expiry without touching the portal.
6. Review privileged directory roles and their members; export for access review.
7. Preview the exact Graph request or PowerShell command an action will run, before running it.
8. Generate an ISO 27001-aligned Evidence Pack for a review period.
9. Run entirely against fixture data (mock mode) for demos, testing, and training.

## 5. Non-goals

- **Not** a replacement for Microsoft's admin portals for one-off visual tasks; it is a complement optimised for power, safety and evidence.
- **Not** a generic Graph API browser or a raw PowerShell terminal (an *advanced console* may come later, clearly labelled risky, off by default).
- **Not** an identity governance suite (no access-review campaigns, PIM policy authoring in v1).
- **Not** an ISO 27001 certification product. Graphdeck provides supporting technical controls and evidence; compliance depends on the organisation's own ISMS, policies, scope and implementation.
- **Not** a monitoring/alerting tool; no background agents in v1.
- **Not** multi-user server software; it is a local operator console (central log forwarding is supported as an output).

## 6. Functional requirements

FR are grouped; "MVP" marks the initial implementation scope.

### FR-1 Authentication & session (MVP: status screen + mock; live device-code)
- Connect to Microsoft Graph via OAuth 2.0 device code (delegated) or client credentials (app-only) where configured.
- Display: connection state, tenant ID, tenant display name (where available), signed-in account, auth mode, active provider(s), granted scopes, module availability, token expiry.
- Detect missing PowerShell modules; offer installation *guidance* — never auto-install without explicit approval.
- Never store passwords; tokens held in memory (optional OS-keyring cache is opt-in, documented).

### FR-2 Action registry (MVP)
- All administrative capability is declared in a typed action registry (see §13). Nothing is hard-coded into UI screens.
- Every action declares: ID, name, description, service area, preferred/supported providers, selection rule, required Graph scopes, admin roles, PowerShell module, app-only/delegated support, beta requirement, risk classification, dry-run support, rollback level, audit requirement, input/output schema, provider templates, docs link.

### FR-3 Provider engines (MVP: Graph REST, PowerShell, Mock; SDK stub)
- Graph REST engine: token acquisition, v1.0/beta, pagination, throttling/retry, batch & delta (post-MVP), error normalisation, redacted logging, request preview, ETag capture.
- PowerShell engine: safe session management, JSON-first output (`ConvertTo-Json`), warning/noise-tolerant parsing, `-WhatIf` passthrough, module detection, injection-safe parameter binding, Windows PowerShell + PowerShell 7 support.
- Graph SDK engine: interface defined; implementation post-MVP (REST covers the same surface initially — see capability matrix).
- Mock engine: full fixture-backed tenant, error/throttle/permission simulation, write mutation so audit/rollback flows are demonstrable offline.
- All engines return the **common result envelope** (§14).

### FR-4 Modules (MVP: Users, Groups, Licences; designed: Exchange, Roles, Apps, Devices, SharePoint, Teams, Audit)
As specified per module in §12.

### FR-5 UX (MVP)
- Dashboard; navigation tree; command palette; global search across module actions; rich sortable/filterable tables; detail views; dialogs; wizard-style write forms; command/request preview; confirmation prompts scaled to risk; inline validation; progress indicators; background task display; toasts; readable error panels; raw output view; logs panel; audit view; change history; rollback view; help panels; themes (Textual built-in); persistent tenant/account/mode context in the status bar.

### FR-6 Write pipeline & change control (MVP)
Every write action passes through: provider resolution → preview → risk calculation → before-state capture → rollback snapshot → (optional) change reason → confirmation → execution → after-state capture → audit event → result validation → rollback availability display. See §16.

### FR-7 Rollback (MVP: model + user property/group membership rollback in mock & live-capable code path)
As specified in §17.

### FR-8 Audit & evidence (MVP: hash-chained JSONL audit log, verification, evidence pack export)
As specified in §15.

### FR-9 Export (MVP)
Export any table/result to JSON, CSV, Markdown, plain text. Exports are deliberate user actions, written to a documented directory, and redacted per policy.

### FR-10 Modes (MVP)
- **Live**: real provider execution.
- **Mock**: fixture data, no network, no PowerShell.
- **Dry-run**: full pipeline including preview/audit-of-intent, but no execution.

## 7. Non-functional requirements

### Security (see also docs/security-model.md)
- No password storage, ever. No token persistence by default.
- Redact secrets/tokens/auth headers/certificate material from every log, preview, export and audit record (deny-list + pattern scrubbing, tested).
- Tenant + account + mode always visible; cross-tenant actions structurally prevented (single active tenant context per session).
- Confirmation gates scaled to risk; destructive/privileged/bulk require explicit typed confirmation.
- No arbitrary command execution by default. Least-privilege guidance shown per action (scopes/roles).
- No automatic elevation, no silent consent to new permissions.
- No external data transmission, no phone-home, no telemetry (opt-in only if ever added, off by default, documented).
- User-controlled purge of local cache and logs.

### Accessibility
- 100% keyboard operable; every action reachable without a mouse.
- High-contrast theme; no information conveyed by colour alone (risk levels also carry text labels).
- Works over SSH and in screen-reader-friendly terminals to the extent the framework allows.

### Performance
- UI remains responsive during provider calls (all I/O async / worker-threaded).
- Tables handle 10,000+ rows via pagination/virtualisation; first page of a Graph list renders < 2s on a typical tenant (network permitting).
- Startup < 1.5s in mock mode.

### Reliability
- One failed operation never crashes the app; errors render as structured panels with recovery guidance.
- Throttling (429/`Retry-After`) honoured with capped retries; retries only for idempotent/safe operations.
- Expired sessions detected and surfaced with a re-auth path.
- Malformed JSON/PowerShell output handled defensively (tested).
- Cancellation supported for long-running list operations.

### Error handling
- All provider errors normalised to one taxonomy (auth, permission, throttled, not-found, conflict, invalid-input, provider-unavailable, network, parse, unknown) with: human message, provider raw detail (redacted), correlation ID, retriability, guidance.

### Auditability & ISO 27001-aligned evidence
- Every action (reads logged as read-events per policy; writes always) produces an audit event capturing who/what/when/tenant/object/provider/exact redacted request/before/after/reason/result/rollback linkage.
- Audit log is append-only JSONL with SHA-256 hash chaining; integrity verifiable via a command; exportable to JSONL/CSV/SIEM-friendly formats; retention configurable.
- Evidence Pack export (§15.4).
- **The product does not claim ISO 27001 compliance.** It provides supporting controls and evidence for an organisation's own ISMS.

### Data handling
- Tenant data never leaves the machine except to Microsoft endpoints the operator invoked.
- Local state in documented locations (`~/.config/msgraph-tui/`, `~/.local/share/msgraph-tui/`).
- Debug logs separated from audit logs; both redacted; retention configurable; purge command provided.

## 8. CLI/TUI interaction model

- `graphdeck` launches the TUI. Flags: `--mock`, `--dry-run`, `--live`, `--config PATH`, `--fixtures PATH`.
- Global keys: `Ctrl+P` command palette, `/` search-in-view, `F1`/`?` help, `Ctrl+L` logs panel, `Ctrl+E` export, `Ctrl+Q` quit, arrows/`Tab` navigation, `Enter` open detail/confirm.
- Left navigation tree (modules → views); main content area; persistent status bar: `MODE | tenant | account | provider`.
- Novice path: navigate tree → guided form → preview → confirm. Expert path: command palette → action → edit params → preview → execute; every screen exposes the underlying operation.

## 9. Backend provider strategy

Provider preference order (global default, overridable globally and per service area):

1. **Microsoft Graph REST API** — first choice wherever coverage is robust (users, groups, licences, applications, service principals, directory roles, devices, sign-in/audit logs, Teams/SharePoint reads).
2. **Official Microsoft Graph SDK** — parity alternative to REST where SDK ergonomics/typing help; engine interface defined in v1, implemented post-MVP.
3. **Microsoft Graph PowerShell SDK** — for admins who standardise on it; also a bridge where SDK snippets are the documented path.
4. **Workload PowerShell modules** — *first-class, not legacy*: Exchange Online (mailbox permissions, forwarding, litigation hold, transport), Teams (policies), SharePoint Online (tenant settings), Security & Compliance. These remain the only practical supported path for significant functionality.
5. **Other official Microsoft APIs** — only where clearly justified and documented.

Selection considers: feature coverage, stability/support status, required permissions, tenant environment, installed modules, auth mode (app-only vs delegated), read vs write, rollback capturability, beta acceptability (off by default), and administrator preference. The user can configure preferences globally and per module. The app is **honest about coverage**: the capability matrix (§10, maintained in `docs/capability-matrix.md`) records where Graph does *not* reach parity and a PowerShell module is required.

## 10. Provider capability matrix (summary)

Full matrix with per-question answers (Graph REST? SDK? Graph PS? workload PS? preferred/most complete/safest/easiest to test/app-only/delegated/rollback/implemented/beta/limitations) lives in [`docs/capability-matrix.md`](capability-matrix.md). Summary:

| Task area | Graph REST | Graph SDK | Graph PS | Workload PS | Preferred | Implemented (MVP) |
|---|---|---|---|---|---|---|
| Users CRUD, licences | ✅ | ✅ | ✅ | — | graph_rest | graph_rest, graph_powershell, mock |
| Groups & membership | ✅ | ✅ | ✅ | Exchange PS (DLs) | graph_rest | graph_rest, graph_powershell, mock |
| Subscribed SKUs / licence reports | ✅ | ✅ | ✅ | — | graph_rest | graph_rest, mock |
| Directory roles | ✅ | ✅ | ✅ | — | graph_rest | designed |
| Apps / service principals | ✅ | ✅ | ✅ | — | graph_rest | designed |
| Mailbox permissions, forwarding, holds | ⚠️ partial/none | ⚠️ | ⚠️ | ✅ ExchangeOnlineManagement | exchange_powershell | designed (engine ready) |
| Teams policies | ⚠️ read-mostly | ⚠️ | ⚠️ | ✅ MicrosoftTeams | teams_powershell for policies | designed |
| SharePoint tenant settings | ⚠️ | ⚠️ | ⚠️ | ✅ Microsoft.Online.SharePoint.PowerShell / PnP | sharepoint_powershell | designed |
| Intune device reads | ✅ (DeviceManagement) | ✅ | ✅ | — | graph_rest | designed |
| Security & Compliance | ❌ mostly | ❌ | ❌ | ✅ SCC PS | scc_powershell | designed |
| Directory audit / sign-ins | ✅ | ✅ | ✅ | — | graph_rest | designed |

## 11. Admin workflow examples

**W1 — Offboard a leaver (Dan):** Users → search → detail → "Disable account" → form pre-filled → preview shows `PATCH /v1.0/users/{id} {"accountEnabled": false}` → risk HIGH → reason + ticket ref → confirm → before/after captured → audit event → rollback available (re-enable with drift check).

**W2 — Licence hygiene (Priya):** Licences → "Disabled users with licences" → table → multi-select → bulk remove wizard → per-object snapshots → typed confirmation (`REMOVE 14`) → progress with per-item results → partial failures listed → export report.

**W3 — Evidence pack (Sofia):** Audit → set period → "Export Evidence Pack" → integrity verification runs → ZIP with manifest, change history JSONL/CSV, high-risk register, rollback records.

**W4 — Provider inspection (Marcus):** Any action → `F2` provider panel: provider chosen and why, alternates, scopes, roles, beta flag, rollback level, docs link; raw request preview always one key away.

## 12. Core modules

(Each module's actions are registry entries per §13; examples listed are the target action set — MVP subset marked ✔.)

1. **Auth & session** ✔ — status screen, connect/disconnect, scope display, module availability, expiry.
2. **Users** ✔ — list/search ✔, detail ✔, licences view ✔, memberships ✔, enable/disable ✔, update properties ✔, assign/remove licence ✔, revoke sessions, export ✔; before-state capture ✔; rollback ✔.
3. **Groups** ✔ — list/search ✔, detail ✔, owners/members ✔, add/remove member ✔ (rollback snapshot ✔), create (guided), update settings, export ✔; role-assignable group warnings ✔.
4. **Exchange & mailboxes** — list mailboxes, detail, permissions view/manage, forwarding, litigation hold/retention, quotas, shared mailboxes, DLs, transport settings; provider: Exchange PS; rollback snapshots for permission/forwarding changes.
5. **Licences** ✔ — SKUs ✔, consumption ✔, users-by-licence ✔, assign/remove ✔, unlicensed users, disabled-with-licence report ✔, export ✔, group-licensing conflict warnings.
6. **Roles & permissions** — directory roles, members, privileged-user search, export, high-privilege highlighting, high-risk change gates, strict rollback sanity checks.
7. **Applications & service principals** — list, permissions & consent grants, owners, credential *metadata only* (never secret values), expiring credentials, high-risk permission flags.
8. **Devices / Intune** — read-only first: list, compliance, ownership, last check-in, stale/non-compliant reports.
9. **SharePoint & OneDrive** — tenant settings, sites, owners, sharing settings, risky-sharing highlights.
10. **Teams** — teams, owners/members, channels, membership export, selected settings.
11. **Audit & logs** ✔ — TUI action history ✔, change history ✔, rollback history ✔, provider request history ✔, error history, directory audit/sign-ins (Graph), evidence pack ✔.

## 13. Action registry contract

Declarative, typed, validated at startup. Full schema in `docs/developer-guide.md`. Every action carries:

`id · name · description · service · preferred_provider · supported_providers · selection_rule · graph template (method/path/query/body, beta flag) · powershell template (module/command/params) · sdk operation · params (name/type/required/default/choices/validation) · input_schema · output_schema · graph_scopes · admin_roles · ps_module · app_only_supported · delegated_required · beta_required · risk (read_only/low/medium/high/destructive) · tags (privileged/bulk/compliance_sensitive) · supports_dry_run/whatif · rollback (level, read-action, inverse builder) · audit_requirement · confirmation (none/confirm/typed) · output parser · ui view type · docs_url`

Registry validation rejects: duplicate IDs, preferred provider not in supported list, write actions without rollback classification or confirmation level, params without types.

## 14. Common result envelope

Every engine returns: success · provider · action id · operation ID · redacted request/command preview · parsed data · raw response (where safe) · warnings · normalised errors · retry info · permission info · audit metadata · rollback snapshot ID · timing · correlation ID · pagination/truncation flags.

## 15. Audit, change control, evidence (ISO 27001-aligned)

### 15.1 What is recorded
Who (UPN + auth mode) · when (UTC) · tenant · object (id/type) · action & provider · exact redacted request/command · before-state · after-state · reason/ticket/requestor/expiry/approval (when policy enabled) · risk level · rollback availability & linkage · validation results · errors/partial failures · interaction mode (interactive/bulk/scripted/imported).

### 15.2 Change-reason policy
Optional "change reason required" policy: when on, writes require reason, ticket/change reference, business owner/requestor, optional expiry for temporary changes, optional approval reference, notes.

### 15.3 Tamper evidence
Append-only JSONL; each entry embeds `prev_hash` and `entry_hash = SHA-256(prev_hash + canonical(entry))`; genesis anchored; `verify` command re-walks the chain and reports first divergence. Export to JSONL/CSV/SIEM formats; optional forwarding sink (config). Audit log **never** stores secrets, tokens, passwords, private keys, or unredacted sensitive values. Debug logs are a separate stream. Retention configurable per stream.

### 15.4 Evidence Pack
One export containing: change history for a period · administrator activity summary · high-risk action register · failed changes · rollbacks · approval references · before/after diffs · provider info · redacted request records · log-integrity verification result · export manifest (tool version, period, hashes).

### 15.5 Compliance disclaimer
ISO/IEC 27001 conformity depends on the organisation's ISMS, policies, controls, scope and implementation. Graphdeck supplies supporting technical controls and evidence — not certification.

## 16. Change management model — write pipeline

1. User selects action → 2. provider resolved → 3. preview shown (request/command, scopes, roles, risk, rollback level) → 4. risk calculated → 5. before-state retrieved (where supported) → 6. rollback snapshot drafted → 7. change reason collected (if policy) → 8. confirmation (typed for high-risk/destructive/bulk) → 9. execute → 10. after-state retrieved → 11. audit event recorded (hash-chained) → 12. result validated → 13. rollback availability shown.

Dry-run mode stops after step 8 and records an *intent* audit event.

## 17. Rollback model

Rollback is a **controlled compensating change**, never a magical undo. Levels: `none · manual-guidance · partial · full · snapshot-restore · recreate` (recreate only where Microsoft APIs support safe restoration and all data was captured).

**Snapshot contents:** tenant, object id/type, provider, operation ID, timestamp, admin identity, before-state of changed properties, original request, proposed inverse request, concurrency marker (ETag/modified timestamp where available), dependencies, non-restorable fields, risk rating, required validation checks.

**Mandatory sanity checks before rollback:** object exists · same type · changed-since-operation detection (any relevant property, membership, licence, permission, role or policy drift) · dependencies present · permissions sufficient · provider/API still valid · would-overwrite-newer-changes check · blast-radius comparison (rollback must not affect more objects than the original) · destructive-rollback flag · approval requirement. If the object changed since the original operation, automatic rollback is **blocked or diff-confirmed** per policy — never blind.

**Rollback UI:** original change summary · before/current/proposed states · diff view · risk warnings · dependencies · provider · underlying request/command · confirmation · optional approval · post-rollback validation. Rollback executes as a **new audited change linked to the original**.

**Not offered casually for:** permanent deletions, actions with external side effects (mail/notifications), security changes whose reversal reintroduces risk, bulk actions without per-object snapshots, changes without captured before-state or verifiable current-state, undocumented/unstable APIs.

Worked examples (user property update, group membership, licence assignment, mailbox permission, forwarding config, role assignment) are specified in [`docs/rollback-guide.md`](rollback-guide.md).

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Graph/PowerShell surface changes break actions | Declarative registry isolates templates; provider version pinning guidance; error taxonomy surfaces deprecations. |
| Admins over-trust rollback | Explicit levels, sanity checks, diff-confirmation, documentation that rollback is compensating change. |
| Secret leakage via logs/exports | Central redaction service on every output path; tested deny-list + patterns; audit reviews. |
| Wrong-tenant operations | Single tenant context per session, persistent display, tenant echoed in every confirmation. |
| Throttling/bulk failure storms | Capped retries honouring `Retry-After`; bulk operations chunked with per-item results. |
| PowerShell injection | No string interpolation of user input into scripts; typed parameter binding + strict quoting; tested with hostile input. |
| Scope creep vs quality | Registry-driven design lets modules ship incrementally; MVP gate below. |
| Local audit log tampering | Hash chaining + verification + optional forwarding to external sink. |

## 19. Milestones

- **M0 — Foundations:** core envelope/registry/redaction/config/errors, mock engine, tests. ✔ MVP
- **M1 — Console:** Textual shell, nav, dashboard, users/groups/licences browse+detail+export, preview modal, logs panel. ✔ MVP
- **M2 — Change control:** write pipeline, audit chain, rollback snapshots + rollback execution (users/groups), evidence pack. ✔ MVP
- **M3 — Live Graph:** device-code auth, Graph REST engine live, throttling/pagination hardening, session screen.  ✔ MVP (code path; requires tenant to exercise)
- **M4 — PowerShell workloads:** Exchange Online module (mailbox permissions/forwarding), module detection UX, WhatIf integration.
- **M5 — Roles/Apps/Devices modules; batch & delta; SDK engine; approval workflow; SIEM forwarding.**
- **M6 — Teams/SharePoint/SCC modules; template/workflow store; multi-tenant profiles; packaging/signing.**

## 20. Definition of Done (MVP)

- All MVP modules function in mock mode without network or tenant.
- Live Graph REST path implemented for users/groups/licences (device-code), guarded by config.
- Every write action: preview, risk gate, before-state, snapshot, audit event, rollback classification — enforced by pipeline, verified by tests.
- Audit chain verification command passes; tampering detected in tests.
- Redaction proven by tests (tokens, JWTs, secrets, auth headers, password fields).
- PowerShell command builder proven injection-safe by tests.
- `pytest` suite green without any tenant; app boots in mock mode; docs complete (install, modes, security model, audit, rollback, extension guides, troubleshooting, limitations, roadmap).

## 21. Success metrics

- Time-to-evidence: auditor-ready change history for a period exported in < 5 minutes.
- Zero secret material found in logs/exports (verified by tests + periodic manual audit).
- ≥ 90% of MVP admin tasks completable keyboard-only without consulting docs (usability check).
- Rollback offered on 100% of eligible write actions; 0 blind rollbacks possible (sanity checks enforced by code).
- New action addable in < 30 lines of registry code + tests (developer-experience metric).

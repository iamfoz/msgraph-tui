# Product Requirements Document — Graphdeck

**A provider-transparent Microsoft 365 administration console for the terminal.**

| | |
|---|---|
| Status | **v2.0 — approved planning baseline** (supersedes v1.0, 2026-07-06) |
| Working title | **Graphdeck** |
| Repository | `msgraph-tui` |
| Date | 2026-07-08 |

**What changed in v2.0.** v1.0 specified and delivered the MVP (Users/Groups/Licences over a provider-based backend with a 13-step write pipeline, hash-chained audit, drift-checked rollback, evidence packs, and mock/dry-run/live modes; 108 tests). v2.0 does three things:

1. **Reconciles spec with reality** — a verified audit of the code found 24 promised-but-undelivered items, 2 genuine defects, and 14 doc/code inconsistencies (§2). These become a tracked debt register (Epic A, §4) instead of silent drift.
2. **Defines the path to feature-complete** across eight product pillars — Epics A–H (§4–§11) — synthesised from a structured multi-lens review (workload coverage, MSP operations, security/IR, compliance, UX/automation, platform engineering, competitive analysis vs CIPP/AdminDroid/M365DSC/Maester/ORCA/k9s) followed by adversarial critique, deduplication and feasibility correction.
3. **Records explicit rejections** (§14) so scope discipline survives contact with enthusiasm.

Every feature carries a stable ID (e.g. `F-SEC-3`) so the roadmap (§13) can reference it. This document supersedes v1.0 but v1.0's foundations — the safety architecture, the write pipeline, the audit/rollback/evidence model — remain the reference for those subsystems.

---

## 1. Product, problem, personas

### 1.1 Product name
**Graphdeck** (chosen in v1): "Graph" — the primary provider family — plus "deck", the console you operate from. Package `msgraph_tui`, command `graphdeck`. Alternatives considered: TenantDeck, m365ctl, Bastion 365, Ledger365.

### 1.2 Problem statement
Microsoft 365 administration is fragmented across at least five surfaces: the M365/Entra/Exchange/Teams/SharePoint/Intune/Purview web portals, Microsoft Graph (REST, SDKs, Graph PowerShell), and workload PowerShell modules. The portals are incomplete, inconsistent, and hide advanced settings; the programmatic surfaces are complete but unguarded — no confirmation semantics beyond `-WhatIf`, no before-state capture, no change record, no rollback plan, easy cross-tenant mistakes, and audit evidence that must be reconstructed manually for ISO 27001 / SOC 2 / change-management purposes.

Graphdeck fills that gap: a keyboard-first terminal console that executes administration through the *best supported Microsoft interface for each task*, always shows the operator exactly what will run, captures before/after state, produces tamper-evident audit evidence, and offers controlled rollback where technically safe.

**Explicit stance:** Graphdeck does **not** hide Microsoft Graph, the SDKs, or PowerShell. It makes them safer, clearer, faster, more discoverable, and more auditable. Every action maps visibly to the provider operation being performed; the request/command preview is a core feature. The product is honest about provider coverage — Graph does not have parity across all workloads, and PowerShell remains a first-class backend where it is the only practical supported path.

### 1.3 Personas
IT/tenant admins (SMB → enterprise), MSP engineers, security & identity engineers, incident responders, compliance/ISMS owners, and tier-2/3 helpdesk. Five carry the design:

- **Priya — MSP escalation engineer.** 40 tenants; terrified of running a command in the wrong tenant. Needs tenant profiles with visual identity, guarded switching, fleet reports, GDAP visibility, per-customer evidence.
- **Marcus — enterprise identity engineer.** Graph expert; the portal hides half of what he needs. Needs raw request visibility, provider choice, beta gating he controls, CA/PIM/app-consent depth, a headless CLI for his pipelines.
- **Sofia — ISMS / compliance manager.** Needs evidence packs, change reasons tied to tickets, approval workflows, tamper-evident history, attestation exports, control mapping.
- **Dan — tier-2 helpdesk.** Runs the same 15 operations daily and occasionally breaks things with copy-pasted PowerShell. Needs guided forms, safe defaults, joiner/leaver runbooks, mailbox permission & auto-reply tooling, BitLocker/LAPS retrieval, and undo for the low-risk slips.
- **Noor — security incident responder** *(new in v2)*. Works BEC and token-theft cases at 2 a.m. Needs containment runbooks (disable + revoke + method purge in one gated flow), tenant-wide inbox-rule/forwarding hunts, sign-in hunting with saved presets, risky-consent review, and an incident mode that stamps every action for the evidence pack.

**Themes:** (A) *Close the gaps* — make v1's promises true; (B) *Breadth* — cover the real M365 admin surface honestly; (C) *Security & IR* — from reporting to guarded response; (D) *Fleet/MSP* — safe multi-tenant operation; (E) *Governance+* — approvals, standards, auditor-ready evidence; (F) *Power & platform* — scriptable, extensible, fast, portable. Epics A–H map to these themes.

## 2. Current-state reconciliation (as-built vs as-specified)

This section is authoritative over any conflicting statement in older docs.

### 2.1 Implemented and solid
Action registry with startup validation; 13-step write pipeline (preview → before-state → snapshot → reason → confirm → execute → after-state → hash-chained audit → validation → rollback availability); dry-run recording `change_intent`; append-only hash-chained JSONL audit with `verify()` and tamper/deletion detection; drift-checked rollback executed as a new linked audited change; evidence pack (directory + manifest with SHA-256 hashes); Graph REST engine (injection-safe builder, v1.0/beta, `@odata.nextLink` pagination with cap, `Retry-After` retries for idempotent GETs, error normalisation, ETag capture, Authorization header proven never to leak); PowerShell engine (injection-safe builder, out-of-process, noise-tolerant JSON parse, stderr classification, module detection, never auto-installs); mock engine (mutable fixture tenant, `_simulate` error injection); provider selection with reasons/skipped trail; central redaction on every output path; export (JSON/CSV/MD/TXT); Textual UI (nav tree, dashboard, browse tables, preview/confirm gate, rollback modal, audit/changes views, session screen, logs panel, help, command palette for screens AND actions); a comprehensive passing test suite.

### 2.2 Promised in v1 docs but NOT implemented (v2 must close or restate)
Tracked as `F-FIX-*` and scheduled in Milestone **M4** (§13):

- **Live sign-in is unreachable.** `MsalDeviceCodeTokenProvider` exists but is never instantiated; there is no sign-in flow on the Session screen, so `graph_rest.is_available()` is always false and **live mode does not actually work end-to-end today**. README/user-guide instructions describe a flow that no code implements. *(Biggest single gap; `F-FIX-1`.)*
- **App-only (client-credentials) auth** does not exist — only the unwired device-code class. The capability matrix's "App-only ✅" columns are aspirational. *(`F-AUTH-2`.)*
- **Live licence bug:** `licenses.users_by_sku` uses `$filter=assignedLicenses/any(x:x/skuId eq {sku_id})`; the builder only substitutes *whole-value* `{param}` placeholders, so **live Graph would receive the literal `{sku_id}`**. Works in mock only because the mock bypasses the template. Violates the "what is previewed is what runs" guarantee. *(`F-FIX-2`; correctness defect.)*
- **Table sorting** — claimed in `FR-5` and a docstring; no sort exists. *(`F-FIX-3`.)*
- **Multi-select / bulk operations** — workflow W2 describes them; nothing exists (`ActionTag.BULK`/`interaction_mode='bulk'` declared but unused). *(`F-UX-1`.)*
- **Registry actions in the command palette** — palette lists only 8 screens; the expert path "palette → action → edit params → preview → execute" is absent. *(`F-FIX-4`.)*
- **Raw output view & structured error panels** — `envelope.raw` is captured but no screen shows it; errors render as one-line toasts, not panels with correlation ID/retriability. *(`F-UX-14`.)*
- **Provider-preference & beta-toggle UI** — config works; there is no Settings screen (errors even say "choose another provider in Settings"). *(`F-UX-8`.)*
- **Retention enforcement / log rotation** — config knobs exist; nothing enforces them. *(`F-COMP-7`.)*
- **Purge command** and **CLI audit-verify command** — promised; neither exists. *(`F-FIX-8`.)*
- **SIEM/forwarding sink** — §5/§15.3 of v1 use present tense; nothing exists. *(`F-COMP-5`.)*
- **`-WhatIf` in dry-run** — preview note claims it is used automatically; `execute()` never passes it and dry-run forces the mock engine. The note is currently false. *(`F-FIX-9`.)*
- **Evidence pack is a directory, not a ZIP; no period-selection UI** (API supports periods). *(`F-FIX-6`.)*
- **ChangeReason fields** requestor/expiry/approval_ref/notes are collected by neither form; only reason+ticket are captured, so approval references are effectively unrecordable. *(`F-FIX-7`.)*
- **Snapshot ETag never populated** — envelope captures it, executor never copies it into the snapshot. *(`F-FIX-5`.)*
- **Server-side search/pagination** — `search`/`top`/`page_size` declared but browse always calls with `{}`; filtering is client-side only. *(`F-UX-15`.)*
- **Audit/Changes views have no filter box** despite "/ works in any table". *(`F-FIX-4`.)*
- Modules marked "designed" with **zero code**: Exchange, Roles, Apps, Devices/Intune, SharePoint, Teams, directory audit/sign-ins; plus within shipped modules: `groups.create`, group settings update, unlicensed-users report, and the **claimed-but-absent group-based-licensing conflict warnings**. *(covered by §5–§9.)*
- **`scc_powershell`** is referenced in code/docs but never registered as a provider instance. *(`F-FIX-10`.)*

### 2.3 Corrected provider/feasibility facts (claims that were wrong)
v2 bakes these in; they materially change several designs:

- **`Search-UnifiedAuditLog` is an Exchange Online PowerShell cmdlet** (`Connect-ExchangeOnline`), **not** a Security & Compliance/`Connect-IPPSSession` cmdlet. The Graph **Audit Log Query API** (`POST /security/auditLog/queries`, v1.0) is the preferred primary path; EXO PS is the fallback.
- **`onPremisesDirectorySynchronization` is Graph v1.0**, not beta (Entra Connect posture, sync feature flags, accidental-deletion thresholds).
- **`/security/labels/retentionLabels` is Graph v1.0 GA** (records management). Retention *policies*, DLP, and label *policies* still require SCC PowerShell.
- **`directoryRoles` and `oauth2PermissionGrants` support delta queries in v1.0.** Only licence data (`subscribedSkus`/assignments) has no delta and needs full refresh.
- **ETag `If-Match` optimistic concurrency barely applies to core directory resources.** Users/groups/servicePrincipals/applications generally do **not** return `@odata.etag` and do **not** honour `If-Match` on PATCH. Real ETag concurrency lives on Outlook resources (messages/events) and `driveItems`. → v2 **downgrades** ETag concurrency to a best-effort enhancement; the primary write-safety mechanism remains the pipeline's re-read + drift check.
- **`Get-MessageTraceV2` covers ~90 days** (max ~10-day span per query; iterate for longer). `Start-HistoricalSearch` is only for bulk/CSV historical export.
- **EXO V3 module (incl. `Connect-IPPSSession`) runs on PowerShell 7 on all platforms.** Windows PowerShell 5.1 is not required for SCC; a 5.1 CI lane is optional field coverage.
- **Sign-in logs via Graph (`/auditLogs/signIns`) are Entra ID P1-gated**, retention **7 days on Free, 30 days on P1/P2**; directory audit (`/auditLogs/directoryAudits`) needs no premium. Honest degradation must handle the Free/P1 split.
- **`signInActivity` server-side `$filter` works** but cannot be combined with most other filter clauses in one query.
- **`DELETE` of authentication methods is v1.0** for common types (phone, Authenticator, FIDO2, TAP, email, software OATH). App-only password reset remains restricted.

### 2.4 Architecture constraints shaping v2 (from the audit)
- **The one-shot `pwsh -Command` model is the key blocker for workload modules.** Exchange/Teams/SPO/SCC cmdlets require a *live authenticated session* that cannot survive process boundaries. v2 must introduce a **persistent PowerShell host** (`F-PLAT-1`) before any of those modules can authenticate.
- **The Executor is the single write gateway** — the right and only place to add bulk plans, four-eyes, change windows, SoD and policy-as-config, so modules cannot bypass them (same principle as registry validation).
- **The UI is intentionally dumb and the whole context is buildable headless** — a scripting/CLI surface is nearly free (`interaction_mode='scripted'` already exists in the audit schema).
- **Client-side-everything tables** will hit the 10k-row NFR; v2 must push filter/pagination server-side per action.
- **Adding a module today also needs a hand-written mock handler** — add a **generic fixture-driven mock resolver** (`F-PLAT-7`) so mock coverage doesn't become the bottleneck.
- **No cross-process locking** on `audit.jsonl`/snapshots — resolve before headless/scheduled/multi-instance use (`F-PLAT-12`).
- **Several contract fields are dormant extension points** (`selection_rule`, `supports_dry_run`, `app_only_supported`/`delegated_required`, `ActionTag`, `PermissionInfo.satisfied`, `audit_metadata`, snapshot `dependencies`/`required_checks`) — v2 features should activate them rather than inventing parallel mechanisms.

## 3. Feature catalog — how to read Epics A–H

Each feature lists an ID, a one-line description, the *honest* preferred provider (Graph v1.0/beta path or the workload PowerShell cmdlet family, noting where only beta or PS exists), priority (P0–P3) and effort (S/M/L/XL). Priorities reflect adversarial re-ranking: several lens-proposed "P0"s were corrected down where they depend on unbuilt foundations (e.g. Intune remote actions need the read module first; four-eyes and SIEM are governance enhancements, not launch blockers).

## 4. Epic A — Close the gaps (make v1 true)

Small, high-value, mostly UI/plumbing; unblocks the live product and restores honesty.

> **Delivered on the current branch:** F-FIX-1 (live device-code sign-in wired),
> F-FIX-2 (embedded `$filter` substitution), F-FIX-3 (column sorting),
> F-FIX-4 (registry actions in palette + audit/change filter box),
> F-FIX-5 (ETag → snapshot), F-FIX-6 (evidence-pack `--zip` + period via CLI),
> F-FIX-7 (full ChangeReason capture), F-FIX-8 (`verify-audit`/`purge` CLI),
> F-FIX-9 (false `-WhatIf` note fixed), F-FIX-10 (`scc_powershell` registered),
> F-FIX-12 (debug-log rotation), F-FIX-13 (role-assignable warning in gate),
> plus the security-audit hardening (audit head anchor, optional HMAC, locking,
> broadened redaction, endpoint validation). Since then: F-AUTH-2 (certificate
> app-only auth, config-driven or via the Session view toggle), F-PLAT-1
> (persistent PowerShell host), F-UX-1 (multi-select bulk plans) and F-COMP-1
> (four-eyes approval). Remaining in this epic: F-FIX-11 (derive browse columns
> from `output_schema`).

| ID | Feature | Prio | Effort |
|---|---|---|---|
| F-FIX-1 | **Wire live device-code sign-in**: sign-in button + device-code modal on Session view building `MsalDeviceCodeTokenProvider` and attaching it to `graph_rest`; then show token expiry, granted scopes (token claims), tenant display name, and a re-auth path for `EXPIRED_SESSION`. Unlocks the entire live product. | P0 | M |
| F-FIX-2 | **Fix `licenses.users_by_sku` `$filter` substitution** (embedded-placeholder support in the builder, with tests) so live Graph receives a valid filter. | P0 | S |
| F-FIX-3 | **Column sorting with persistence** on browse tables (`s` cycles/reverses; sort saved per view). | P0 | S |
| F-FIX-4 | **Palette & filter completeness**: register every read/write action as a command-palette entry; add a filter box to Audit/Changes views. | P0 | S |
| F-FIX-5 | **Copy ETag into rollback snapshots** (`concurrency_marker`); use best-effort on write where the resource honours `If-Match`. | P1 | S |
| F-FIX-6 | **Evidence pack UX**: period-selection modal + write as a single ZIP. | P1 | S |
| F-FIX-7 | **Full ChangeReason capture** (requestor, approval_ref, expiry, notes) in the confirm gate → richer evidence packs. | P1 | S |
| F-FIX-8 | **`graphdeck verify-audit` and `graphdeck purge [--logs|--all]` CLI** subcommands. | P0 | S |
| F-FIX-9 | **Remove/repair the false `-WhatIf` preview note**; wire real `-WhatIf` only where the provider path supports it (M5). | P0 | S |
| F-FIX-10 | **Register `scc_powershell` provider** (with `Connect-IPPSSession` hint). | P1 | S |
| F-FIX-11 | **Derive browse columns from `output_schema`**; set `object_id`/`object_type` on read audit events. | P2 | S |
| F-FIX-12 | **Debug-log rotation** honouring `debug_log_retention_days`. | P2 | S |
| F-FIX-13 | **Role-assignable-group warning in the write confirm gate** for `groups.member_add/remove`. | P1 | S |

## 5. Epic B — Workload coverage

Graph-only where Graph suffices; workload PowerShell where it is the only supported path (these depend on `F-PLAT-1`).

### 5.1 Entra ID / identity
| ID | Feature | Provider (honest) | Prio |
|---|---|---|---|
| F-ENT-1 | **Conditional Access explorer** — CA policies as a readable matrix (state incl. report-only, conditions, grant/session controls), named-locations sub-view, guarded state toggle. | Graph v1.0 `/identity/conditionalAccess/policies`,`/namedLocations` | P0 |
| F-ENT-2 | **Authentication methods suite** — registration report; role-joined **MFA-gap report**; per-user method list + reset; TAP issuance; methods-policy view. | Graph v1.0 `/reports/authenticationMethods/userRegistrationDetails` (P1-gated; degrade on Free), `/users/{id}/authentication/methods` (DELETE v1.0) | P0 |
| F-ENT-3 | **PIM posture** — eligible vs active schedules per role/principal, expiring-assignment report, activation where feasible. | Graph v1.0 `/roleManagement/directory/*ScheduleInstances`,`*ScheduleRequests` | P1 |
| F-ENT-4 | **Administrative units** — browse (incl. restricted-management), members, scoped roles, snapshot-rollback membership edits. | Graph v1.0 `/directory/administrativeUnits` | P1 |
| F-ENT-5 | **Dynamic group rule viewer/editor** — show/edit `membershipRule` with client-side syntax validation, pause/resume. (No live membership-preview API.) | Graph v1.0 group PATCH | P2 |
| F-ENT-6 | **Cross-tenant access settings** — default + per-partner B2B/direct-connect + trust as a diff table. | Graph v1.0 `/policies/crossTenantAccessPolicy` | P1 |
| F-ENT-7 | **B2B guest lifecycle** — guests with invite state/sponsor/last sign-in, stale-guest report, re-invite, guarded bulk cleanup. | Graph v1.0 (`signInActivity` P1; degrade on Free) | P1 |
| F-ENT-8 | **Directory sync (Entra Connect) posture** — hybrid?, last sync + staleness, PHS, on-prem provisioning errors. | Graph **v1.0** `/directory/onPremisesSynchronization` | P2 |
| F-ENT-9 | **User creation / joiner flow** — guided create, usage location, licence (direct or via group), memberships, optional TAP; mirror of offboarding. | Graph v1.0 | P1 |
| F-ENT-10 | **Domain lifecycle + federation-backdoor detection** — add/verify domains with copy-out DNS, default-domain management, **read domain federation settings** (persistence vector). | Graph v1.0 `/domains`,`/domains/{id}/federationConfiguration` | P2 |

### 5.2 Exchange Online (workload PS; `F-PLAT-1` dependency)
| ID | Feature | Provider | Prio |
|---|---|---|---|
| F-EXO-1 | **Mail flow configuration suite** — transport rules (priority order + conditions/actions), connectors, accepted domains; guarded edits. | EXO PS `*-TransportRule`,`*-InboundConnector`,`*-OutboundConnector` — no Graph parity | P0 |
| F-EXO-2 | **Mail flow investigation** — message trace + delivery drill-down; quarantine triage/release. | EXO PS `Get-MessageTraceV2` (~90-day lookback, ≤10-day span/query), `*-QuarantineMessage` | P0 |
| F-EXO-3 | **Helpdesk mailbox pack** — auto-reply on behalf, calendar/folder permission grant/audit, **shared-mailbox conversion** (licence-aware >50 GB warning). Highest-frequency tickets. | EXO PS (+ some Graph `mailboxSettings`) | P0 |
| F-EXO-4 | **Distribution lists & mail-enabled security groups** — invisible to Graph `/groups`: classic + dynamic DLs, membership, moderation, send-as restrictions. | EXO PS `*-DistributionGroup`,`*-DynamicDistributionGroup` | P1 |
| F-EXO-5 | **Protection policy viewer (EOP/Defender for Office)** — anti-spam/-phish/-malware, Safe Links/Attachments with scoping + preset-drift report. | EXO PS | P2 |
| F-EXO-6 | **MRM retention tags/policies & journal rules**; assign policy + `Start-ManagedFolderAssistant`. | EXO PS | P2 |
| F-EXO-7 | **Resource mailboxes & client access** — room calendar processing, mobile devices/wipe, CAS settings. | EXO PS | P2 |
| F-EXO-8 | **Email authentication posture** — per-domain SPF/DMARC via local DNS (no write), DKIM state (`Get-DkimSigningConfig`), enable + `Rotate-DkimSigningConfig` as audited writes. Post-2024 bulk-sender relevance. | EXO PS + local DNS | P1 |

### 5.3 Teams / SharePoint / Intune / Purview / Service health

> **Delivered (first slice):** F-TEAM-1 — meeting/messaging policy browse,
> per-user policy lookup and a rollback-capable *grant meeting policy*;
> F-SPO-1 — site inventory/detail and a high-risk, rollback-capable *set
> sharing capability*; Purview — read-only retention and DLP policy browse
> (DLP in test mode flagged). All run on the persistent PowerShell host in live
> mode and on fixtures offline; none has yet been validated against a live
> tenant. Diff-vs-Global, calling/app policies, quota/lock writes, deleted-site
> restore and hubs remain open.
| ID | Feature | Provider | Prio |
|---|---|---|---|
| F-TEAM-1 | **Teams policy management** — meeting/messaging/calling/app policies, diff-vs-Global, bulk assignment, federation/external-access. | MicrosoftTeams PS (policy CRUD); Graph for inventory | P1 |
| F-TEAM-2 | **Team lifecycle** — archive/unarchive, ownerless/single-owner report + add-owner, expiration policy. | Graph v1.0 `POST /teams/{id}/archive`,`/groupLifecyclePolicies` | P1 |
| F-SPO-1 | **SharePoint site lifecycle** — inventory (storage/quota/sharing/lock/hub), quota+lock+sharing writes, deleted-site restore, hubs. | Split: Graph `/sites` inventory; SPO PS writes/restore | P1 |
| F-SPO-2 | **Sharing-links audit** — cancellable background crawl for anonymous/org-wide links. | Graph v1.0 drive items + `/permissions` | P2 |
| F-INT-1 | **Intune inventory** — config profiles, compliance policies, assignments, per-policy deployment status, Autopilot. | Graph v1.0 `/deviceManagement/*` (some reports beta — gate honestly) | P1 |
| F-INT-2 | **Intune remote actions** — sync/restart (low), lock (high), retire/wipe (destructive) via the pipeline. *Sequenced after F-INT-1.* | Graph v1.0 `/managedDevices/{id}/{syncDevice\|rebootNow\|remoteLock\|retire\|wipe}` | P1 |
| F-INT-3 | **BitLocker & Windows LAPS retrieval** — audited, **display-once** secret reads (never persisted, never in audit body — only the fact of retrieval is logged). | Graph v1.0 `/informationProtection/bitlocker/recoveryKeys`,`/directory/deviceLocalCredentials` | P1 |
| F-PUR-1 | **Purview policy management** — retention policies/labels, DLP policies/rules with mode, "still in test" report. | SCC PS for policies/DLP; **retention *labels* Graph v1.0** `/security/labels/retentionLabels` | P1 |
| F-PUR-2 | **Unified Audit Log search workspace** — async job (query → poll → browse/export), IR presets, eDiscovery visibility. | **Graph Audit Log Query API v1.0** (primary); **EXO PS `Search-UnifiedAuditLog`** (fallback) — *not SCC* | P1 |
| F-SVC-1 | **Service health & message center** — health per workload + incidents/advisories timelines; message-center reader (action-required filter), mark-read/archive, CAB export. | Graph v1.0 `/admin/serviceAnnouncement/*` | P1 |

## 6. Epic C — Security & incident response

| ID | Feature | Provider | Prio |
|---|---|---|---|
| F-SEC-1 | **Risky OAuth consent hunter** — join SPs + delegated grants + app-role assignments, risk-scored, guarded revoke. | Graph v1.0 `/servicePrincipals`,`/oauth2PermissionGrants`,`/appRoleAssignedTo` | P0 |
| F-SEC-2 | **Malicious inbox rule & forwarding hunt** — sweep every mailbox for BEC-pattern rules + external forwarding; flag + guarded remediate; quarantine. | EXO PS (`F-PLAT-1`) | P0 |
| F-SEC-3 | **Compromised-account containment runbook** — disable → revoke → reset → purge/reset MFA as one gated unit, shared incident reason + single typed confirm, per-step audit/rollback. (Built on `F-UX-2`.) | Graph v1.0 | P0 |
| F-SEC-4 | **Bulk session revocation** with staged execution from any user table/group. | Graph v1.0 `revokeSignInSessions` | P0 |
| F-SEC-5 | **Sign-in hunt console** — structured filter builder + saved presets; **legacy-auth usage** flagship preset. | Graph v1.0 `/auditLogs/signIns` (P1; 7-day Free / 30-day P1; risk fields P2) | P1 |
| F-SEC-6 | **Identity Protection module** — risky users + detections, confirm-compromised/dismiss. | Graph v1.0 `/identityProtection/*` (P2) | P1 |
| F-SEC-7 | **Credential expiry sweep** — all app/SP secrets + key creds + SAML certs, bucketed by horizon, owners joined. | Graph v1.0 (metadata only; values never retrievable) | P1 |
| F-SEC-8 | **Service principal posture diff over time** — content-addressed snapshots of roles/grants/cred-metadata/owners/redirect URIs; diff two points. | Graph v1.0 | P1 |
| F-SEC-9 | **Privileged role posture (PIM-aware)** — permanent-active vs eligible, holders without MFA, stale privileged accounts. | Graph v1.0 role management + PIM schedules | P1 |
| F-SEC-10 | **Secure Score view** — current + stored trend + per-control drill-down. | Graph v1.0 `/security/secureScores` | P2 |
| F-SEC-11 | **App-consent governance** — pending requests, tenant consent posture, permission-grant policies, guarded "tighten" write. | Graph v1.0 `/identityGovernance/appConsent/*`,`/policies/authorizationPolicy` | P2 |
| F-SEC-12 | **Break-glass account monitor** — register IDs; on-demand posture check (any sign-in = alarm, CA-exclusion, MFA, still privileged). | Graph v1.0 (sign-in read P1) | P1 |
| F-SEC-13 | **Defender XDR incident/alert triage** — `/security/incidents` + `/security/alerts_v2`, status/assignment/classification writes. | Graph v1.0 (`SecurityIncident.ReadWrite.All`) | P1 |
| F-SEC-14 | **Incident mode** — session-scoped incident ID in the status bar; pre-fills reason/ticket; scoped IR evidence pack. | Local (executor/audit/evidence) | P1 |

## 7. Epic D — Fleet / MSP multi-tenant

**Design decision (§16):** default to *serial* tenant switching (teardown + rebuild context), honouring v1's single-active-tenant invariant; a warm token pool is an opt-in P1 enhancement, not the default.

| ID | Feature | Provider | Prio |
|---|---|---|---|
| F-MT-1 | **Tenant profile store** (supersedes single `tenant_id`/`client_id`) — per-alias JSON: id, display name, tags, colour, production flag, cloud environment, per-profile auth + provider prefs; absorbs "named environments". | Local; name confirmed via `GET /organization` | P0 |
| F-MT-2 | **Guarded fast tenant switcher** — `Ctrl+T` fuzzy picker; switch requires tenant-name typed-confirm; per-tenant colour skin tints status bar/borders/modals. | Local (+ MSAL per-tenant caches) | P0 |
| F-MT-3 | **Per-tenant state partitioning** — `state_dir/tenants/<id>/{audit,snapshots,exports}/`: independent chain + rollback store + evidence; migration tool splits existing chain. | Local | P0 |
| F-MT-4 | **Write-plan tenant binding guard** — stamp every plan with the tenant captured at plan time; commit refuses if the active tenant changed. | Local (executor) | P0 |
| F-MT-5 | **Fleet executor + cross-tenant reports** — fan any read action across profiles with bounded concurrency + per-tenant rate budgets; canned fleet reports. | Graph v1.0 reads | P1 |
| F-MT-6 | **Cross-tenant object search** — find a UPN/email/name across all/tagged profiles. | Graph v1.0 | P2 |
| F-MT-7 | **GDAP module** — delegated-admin relationships + access assignments, map action `admin_roles` to granted GDAP roles (preflight), expiry/coverage report. | Graph v1.0 `/tenantRelationships/delegatedAdminRelationships` | P1 |
| F-MT-8 | **Bulk-across-tenants writes** — one FleetWritePlan = one full WritePlan per tenant (own snapshot + audit in its chain), canary rings, per-tenant gates, abort-on-threshold. | Reuses each tenant's engines | P1 |
| F-MT-9 | **Tenant tags & saved selection sets** — resolved list recorded in fleet audit events for exact evidence. | Local | P2 |
| F-MT-10 | **Multi-tenant mock fleet fixtures** — several fixture tenants + per-tenant `_simulate`. | Mock engine | P1 |
| F-MT-11 | **Migration workbench** — read-only cross-tenant mailbox migration status, endpoint config, snapshotted resumable UPN/domain-rename waves. | EXO PS + Graph | P3 |

## 8. Epic E — Governance, compliance & change control

> **Delivered:** F-COMP-1 in full. Opt-in per risk level
> (`require_approval_for_risk`). A change request is bound to a SHA-256 content
> hash of action + parameters + tenant, re-derived before signing; the approver
> must differ from both the requester and the executor; bulk runs need one
> approval covering every object; rollbacks are exempt; applied requests cannot
> be replayed; all decisions are audited. Signing: per-approver Ed25519 keys
> checked against an organisation trust store (`graphdeck keygen`), with a
> shared HMAC key as a lighter option. Channels: local files, offline
> export/sign/import, git branches reviewed as PRs, and webhook notifications.

| ID | Feature | Provider | Prio |
|---|---|---|---|
| F-COMP-1 | **Four-eyes approval workflow** — split at the `plan_write`/`commit_write` seam: requester emits a hash-identified Change Request; approver reviews before/after + preview and signs (local Ed25519); execution requires a valid token. Channels: file, git/PR, webhook. | Local (Ed25519) | P1 |
| F-COMP-2 | **Policy-as-config guardrail file** — `policy.toml` overriding registry defaults (raise confirmation, require approval for classes, restrict providers), enforced in the Executor. Activates dormant `selection_rule`/tags. | Local | P1 |
| F-COMP-3 | **Change windows & freeze calendars** — maintenance windows / freeze periods as a pipeline gate. Import direction only (no ICS export). | Local (RFC 5545 subset) | P1 |
| F-COMP-4 | **Segregation-of-duties engine** — warn/block self-targeting, approver==requester, configurable role-conflict pairs. (App-only has no "operator" — document.) | Local + Graph `/me` | P1 |
| F-COMP-5 | **SIEM/syslog/webhook forwarding** — RFC 5424 syslog/TLS, CEF, HTTP webhook (Splunk HEC + generic) as a post-append hook on `AuditLog.append` (preserves local chain). | Local networking | P1 |
| F-COMP-6 | **External immutable anchoring** — periodic signed anchor (head hash + count + time) to write-once storage (Azure Immutable Blob / S3 Object Lock / RFC 3161 TSA). | Non-Graph HTTP | P2 |
| F-COMP-7 | **Retention enforcement with chain-safe rotation** — `graphdeck retention run`; segment the chain so pruned segments stay verifiable via anchors. Co-designed with F-COMP-6. | Local | P2 |
| F-COMP-8 | **Signed evidence packs + standalone verifier** — Ed25519-sign the manifest; ship a tiny verifier; optional RFC 3161 timestamp. | Local (cryptography) | P1 |
| F-COMP-9 | **PII-minimisation / pseudonymisation export profiles** — `full`/`minimised`/`pseudonymised` layered on redaction. | Local | P2 |
| F-COMP-10 | **Scheduled attestation exports** — declarative recurring exports via headless CLI for external schedulers. | Graph v1.0 reads | P1 |
| F-COMP-11 | **Control-mapping annotations** — org-editable file mapping evidence to ISO 27001:2022 Annex A / SOC 2 CC; a starting template, not a compliance claim. | Local | P2 |
| F-COMP-12 | **Break-glass emergency change mode** — policy-defined bypass of approval/window gates; typed justification + incident ref, UI paint, mandatory post-hoc review. | Local | P2 |
| F-COMP-13 | **Temporary-change expiry tracker** — surface `ChangeReason.expiry` nothing re-reads: dashboard widget + standing-exception view, one-click rollback/review. | Local | P2 |
| F-COMP-14 | **Change bundles** — group several planned writes under one reason/ticket/approval with per-item snapshots/results. | Local | P2 |
| F-COMP-15 | **Shift-handover / on-call digest** — one-key window digest from the chain + directory audits: changes by risk, running/paused jobs, pending approvals, unreviewed emergency changes, expiring temporary changes. | Local (+ Graph directory audit) | P2 |
| F-COMP-16 | **As-built tenant documentation generator** — render config snapshots into versioned Markdown/HTML (org, domains, CA policies in plain language, roles, licensing, mail-flow topology). | Local over snapshot data | P2 |

## 9. Epic F — Standards, baselines & desired-state

| ID | Feature | Provider | Prio |
|---|---|---|---|
| F-STD-1 | **Standards & baseline engine** — declarative assertions over registry read actions → drift matrix + alignment score; org baselines + public packs (**EIDSCA, CIS M365, CISA SCuBA, ORCA-equivalent EXO**); licence-aware skipping; honest per-assertion coverage. | Graph v1.0 policy reads + EXO/Teams/SPO PS | P0 |
| F-STD-2 | **Failed check → pre-filled remediation** — each assertion may declare a remediation write-action + param template from the failing evidence; `f` opens the normal confirm gate. *Behind F-STD-1.* | Existing/future write actions | P1 |
| F-STD-3 | **Config snapshots + diff** — canonical redacted JSON bundle over a read-action set (manifest hash); diff snapshot↔snapshot, snapshot↔live, tenant↔tenant. Needs a canonicalisation layer. | Same read surface as F-STD-1 | P1 |
| F-STD-4 | **Desired-state templates as reviewable change plans** — diff a template vs live, compile the delta into an ordered, approvable plan. *Superset of F-STD-1/3; land those first.* Hard parts: canonicalisation + dependency ordering. | Reuses all providers | P2 |
| F-STD-5 | **Licence cost optimisation** — overlapping service-plan detection, duplicate direct+group assignments, unused-service-plan report, reclaim workflow; **group-based-licensing error report** (`licenseAssignmentStates` violations) + reprocess — the silent GBL failure v1 falsely claimed to warn about. | Graph v1.0 | P1 |
| F-STD-6 | **Unified recovery hub** — one view over `/directory/deletedItems` (users, groups/teams, apps), SPO deleted sites, soft-deleted mailboxes (EXO PS): everything restorable, days-remaining, one guarded restore/permanent-delete flow. | Graph v1.0 + SPO/EXO PS | P1 |

## 10. Epic G — Power-user UX & discoverability

| ID | Feature | Prio | Notes |
|---|---|---|---|
| F-UX-1 | **Multi-select + bulk change plans** (closes W2) — `Space` toggles selection, one wizard plans N per-object WritePlans (each own snapshot), aggregate preview, typed `REMOVE n`, per-item progress + partial-failure list. | P0 | Executor `BulkPlan`; pairs with `F-PLAT-2` + `$batch`. |
| F-UX-2 | **Runbook engine** — declarative action chains with parameter piping, one approval, per-step audit/rollback; ships containment (F-SEC-3), leaver-offboarding, tenant on/off-boarding as content. v2 scope: sequential chains, no branching. | P0 | Registry construct + executor support built once. |
| F-UX-3 | **Headless CLI** — `graphdeck run/plan/rollback/check/report/attest` through the same pipeline (auth, redaction, audit, exit-code taxonomy); `interaction_mode='scripted'`. No resident scheduler (use cron/Task Scheduler). | P0 | Nearly free; needs app-only auth (`F-AUTH-2`). |
| F-UX-4 | **Watch mode** — `w` auto-refresh with delta highlighting; aggregate read-audit to avoid spam. | P1 | Graph delta queries are the efficient upgrade. |
| F-UX-5 | **Saved views** — named filter+sort+column layout pinned under the module. | P1 | Client-side first. |
| F-UX-6 | **Table ergonomics pack** — hide/reorder columns (incl. beyond spec via `$select`), sticky key column, row pinning, persisted. | P1 | |
| F-UX-7 | **Quick copy-as** — `y` copies row/selection as JSON, CSV, equivalent Graph request, or Graph PowerShell (free from the builders). | P1 | Reinforces transparency. |
| F-UX-8 | **Settings screen** — view/edit provider preferences (global + per-service/action), beta toggle, reason policy, page size; round-trips unknown config keys. | P1 | Config write path doesn't exist yet. |
| F-UX-9 | **Undo toast** — after a LOW-risk write with FULL rollback, 15s "press U to undo" into the sanity-checked rollback path. | P1 | |
| F-UX-10 | **Fuzzy global object search** — `Ctrl+K` across warmed users/groups/SKUs; `$search` live upgrade. | P1 | |
| F-UX-11 | **Object relationship "xray"** — `x` lazily expands user→groups→roles→licences (direct vs group-inherited)→devices→app roles. | P2 | Graph v1.0 `transitiveMemberOf`,`licenseAssignmentStates`. |
| F-UX-12 | **Command mode + resource aliases** — `:` command line (`:users priya`, `:audit 7d`). | P2 | Add `aliases` to action metadata. |
| F-UX-13 | **Object deep-links** — `o` opens the object in the right web portal via URL templates. | P2 | Blade URLs undocumented — data-file them. |
| F-UX-14 | **Raw output + structured error panels** — surface `envelope.raw` and normalised errors. | P1 | Closes a v1 promise. |
| F-UX-15 | **Server-side search/pagination + table virtualisation** — pass `search`/`top`, use `page_size`, debounce filtering, window 100k rows; CI-enforced perf budgets. | P1 | Pairs with delta cache (`F-PLAT-3`). |
| F-UX-16 | **Command templates** — save a completed write form (action + non-secret params + reason boilerplate) as a named favourite; validated against the registry. | P1 | |
| F-UX-17 | **Keymap configuration** — override app/view/RowOp bindings + hotkeys bound to registry actions with preset params. | P2 | Rework RowOp dispatch. |
| F-UX-18 | **Theme packs** — dark/light, true high-contrast, deuteranopia/protanopia-safe risk palettes. | P1 | Badges already carry text labels. |
| F-UX-19 | **Screen-reader accessibility programme** — NVDA/JAWS/VoiceOver-tested flows; linear row-by-row table reading mode; risk level + typed-phrase announced in confirmations. | P1 | Makes the accessibility NFR real. |
| F-UX-20 | **Leaver data-handoff pack** — mailbox→shared conversion (licence-aware), manager delegate, auto-reply, OneDrive transfer. | P1 | EXO PS + Graph. |
| F-UX-21 | **Change-plan collaboration & handover** — comments on a serialised plan; reassign a draft/half-run job with context; shared plan library. | P2 | Complements F-COMP-1. |

## 11. Epic H — Platform, extensibility & distribution

| ID | Feature | Prio | Notes |
|---|---|---|---|
| F-PLAT-1 | **Persistent PowerShell host + session pooling** — pooled long-lived host per module family over length-framed stdin/stdout; **prerequisite for every workload-PS module**. PS7 all-OS; noise-tolerant parsing; optional 5.1 CI lane. | P0 | Single most impactful platform investment. |
| F-AUTH-2 | **Certificate-based app-only auth** — `ConfidentialClientApplication` with cert (keys never logged/audited); `.default` scope; pass-through for workload PS app-only. Enables unattended/headless + fleet. | P0 | Fulfils "app-only ✅". |
| F-PLAT-2 | **Resumable job queue** — `Job` records (SQLite/JSONL) wrapping bulk/runbook ops: per-item status, operation-id links, pause/resume/cancel, progress persistence. | P1 | Above the Executor; pairs with `$batch`. |
| F-PLAT-3 | **Graph `$batch` + delta cache** — coalesce ≤20 requests/`$batch` with per-item Retry-After; delta-sync users/groups/apps/SPs/**directoryRoles**/devices into local SQLite for instant search/sort. (Licence data: full refresh.) | P1 | Corrects v1's "directoryRoles no delta". |
| F-PLAT-4 | **Keyring token cache (opt-in)** — MSAL cache via OS keyring (Keychain/Credential Manager/libsecret) + encrypted-file fallback; sign-in survives restarts. | P1 | Fulfils a v1 FR-1 promise. |
| F-PLAT-5 | **Machine-readable action catalog + generated docs** — `graphdeck catalog --format json|md` against a published JSON Schema; auto-generate capability matrix/docs. | P1 | Feeds F-PLAT-9. |
| F-PLAT-6 | **CI matrix** — GitHub Actions (none today): ruff+mypy + pytest across Ubuntu/macOS/Windows × Python 3.11–3.13; PowerShell 7 lanes exercising builder/parser/host; optional 5.1 lane. | P0 | Guards the "no tenant" invariant as the surface grows. |
| F-PLAT-7 | **Generic fixture-driven mock resolver** — path/collection-templated mock handlers so new modules get mock coverage without a hand-written handler each. | P1 | Removes the module-growth bottleneck. |
| F-PLAT-8 | **Two-tier extensibility** — (1) signed *declarative* action packs (default community story; needs a declarative inverse/transform DSL since those fields are Python callables today); (2) in-process *code* plugins via entry points as a separated, default-deny advanced tier. | P2 | Resolve trust models explicitly. |
| F-PLAT-9 | **Graph API drift contract tests** — nightly CI validates every `GraphTemplate` against Microsoft's published `msgraph-metadata` OpenAPI; flags deprecations. | P2 | Consumes F-PLAT-5. |
| F-PLAT-10 | **Packaging matrix** — PyPI + pipx/uv; PyInstaller one-file binaries (Win/mac/Linux) with SHA-256; winget/brew. | P2 | Bundle fixtures; PowerShell stays external. |
| F-PLAT-11 | **Telemetry-free crash diagnostics + `graphdeck doctor`** — local redacted crash bundle; doctor reports provider availability, module detection, auth state, config sanity. | P2 | No phone-home. |
| F-PLAT-12 | **Config profiles / cross-process locking** — named profiles via `--profile`/env; advisory lock on `audit.jsonl`/snapshots for safe multi-instance + scheduled CLI. | P1 | Blocks safe headless/fleet concurrency. |
| F-PLAT-13 | **Sovereign / national cloud support** — per-profile cloud environment: login authority + Graph base URL for **GCC High, DoD, 21Vianet**. | P1 | Config + endpoint selection; no hot-path code. |

## 12. Cross-cutting requirement updates

- **Authentication (revises v1 FR-1):** device-code (`F-FIX-1`), certificate app-only (`F-AUTH-2`), keyring cache (`F-PLAT-4`), per-profile authority + sovereign clouds (`F-PLAT-13`). Session screen must show token expiry, granted scopes from claims, tenant display name, and a re-auth path.
- **Concurrency safety:** primary mechanism stays *re-read + drift check*; ETag `If-Match` is best-effort only where honoured (§2.3). Add cross-process locking (`F-PLAT-12`).
- **Performance (revises NFR):** server-side filter/pagination + virtualised tables + delta cache; CI-enforced budgets.
- **Accessibility (revises NFR):** a tracked programme with screen-reader testing and a linear table-reading mode (`F-UX-19`), plus colour-blind-safe themes (`F-UX-18`).
- **AI-assist boundary statement** *(new, cross-cutting):* ship a documented stance — **no tenant data leaves the machine to hosted models; AI never composes, fills, or confirms a write; any future assist is opt-in, offline-or-explicitly-configured, and redacted.** Stakes out the position now while competitors bolt on Copilot; aligns with no-phone-home and reassures the compliance persona.
- **i18n:** *deferred* (§14) except one invariant encoded now — action IDs, typed-confirmation phrases, and audit content stay canonical English regardless of UI locale.

## 13. Revised milestone roadmap

Each milestone is shippable and preserves the "no tenant, no network" test invariant.

- **M4 — Truth & live (close the gaps).** `F-FIX-1..13`, `F-AUTH-2`, `F-UX-8/14`. Outcome: live mode actually works; every v1 doc claim is true or restated; app-only auth exists.
- **M5 — Platform for breadth.** `F-PLAT-1` (persistent PS host — gates all workload PS), `F-PLAT-6` (CI), `F-PLAT-7` (mock resolver), `F-UX-1/2/3` (bulk, runbooks, headless CLI), `F-PLAT-12`.
- **M6 — Entra & security core.** `F-ENT-1/2/9`, `F-SEC-1/3/4/14`, `F-STD-1` (standards engine), `F-STD-5` (licence/GBL), `F-UX-15`.
- **M7 — Exchange & helpdesk.** `F-EXO-1/2/3/4/8`, `F-SEC-2`, `F-UX-20`, `F-INT-3`.
- **M8 — Fleet/MSP.** `F-MT-1..8/10`, `F-COMP-10`, `F-PLAT-13`.
- **M9 — Governance+.** `F-COMP-1..5/8`, `F-STD-2/3`, `F-SEC-5/6/7/9`, `F-PLAT-3`, `F-UX-4`.
- **M10 — Depth & distribution.** `F-TEAM-*`, `F-SPO-*`, `F-INT-1/2`, `F-PUR-1/2`, `F-SVC-1`, `F-STD-4/6`, `F-COMP-6/7/9/11..16`, `F-PLAT-8/9/10/11`, remaining `F-UX-*`, `F-SEC-10/11/12/13`, `F-ENT-3..8/10`, `F-EXO-5/6/7`, `F-MT-9/11`.

## 14. Non-goals for v2 (explicit rejections, with rationale)

- **Resident alerting/notification engine** — needs Graph change-notification webhooks (a public HTTPS endpoint the tool can't honestly host) or continuous polling (a daemon the product deliberately is not). Provide the *inputs* instead: SIEM forwarding (`F-COMP-5`) and headless `check` (`F-UX-3`).
- **Canary/honeytoken account kit** — fake directory accounts are a governance liability for the compliance persona, and detection degrades to check-when-open without background agents.
- **Keyboard macro record/replay** — an L-effort command-bus refactor whose value is covered by saved views + command templates + hotkeys + `:` aliases.
- **Full i18n / localisation** — English-first persona; ongoing per-string tax for near-zero v2 demand. Keep only the canonical-English invariant (§12).
- **Opt-in update check** — pipx/winget/brew already surface updates; a network call in a no-phone-home tool buys trust questions for little value.
- **Structured session recording / decision replay** — employee-monitoring optics and its own retention/redaction class; incident mode + the audit chain already answer "what did the operator do and approve".
- **Change-calendar ICS *export*** — keep freeze-calendar *import* (`F-COMP-3`); a local ICS file to ingest is a workflow nobody has.
- **First-run guided tour** — mock mode + a README screencast achieve the "aha" without a coach-mark engine to maintain.
- **Deferred (not rejected):** desired-state templates (`F-STD-4`, behind the standards engine), custom security attributes, Teams Phone/E911, migration workbench (`F-MT-11`), EDU/frontline SKU packs, keyboard macros.

## 15. Definition of Done (v2 additions) & success metrics

**DoD additions:**
- Live mode completes an end-to-end signed-in read *and* an audited write in CI against a recorded-cassette or emulated Graph (no real tenant).
- Every workload-PS module runs against the persistent host in a PS7 CI lane with fixture cmdlet output.
- Multi-tenant: switching tenants provably re-scopes audit/snapshots/exports (per-tenant chains each verify independently); wrong-tenant writes are structurally blocked by `F-MT-4`.
- Standards engine ships ≥1 public framework pack (EIDSCA) with per-assertion evidence and honest "not evaluated (needs P1 / needs PS module)" states.
- Headless CLI runs any registry action with the full pipeline and a documented exit-code taxonomy; app-only auth works unattended.
- No secret material in logs/exports/audit/crash bundles (existing invariant extended to new surfaces, tested).

**Success metrics (v2):**
- Time-to-evidence unchanged (<5 min) but now *per tenant* across a fleet.
- ≥80% of the "daily driver" ticket types (password/MFA reset, mailbox auto-reply, calendar perms, DL membership, licence assign/reclaim, BitLocker/LAPS) completable keyboard-only.
- Standards alignment score computable for a tenant in <60s (cached reads).
- A new *workload* module addable in <1 day given the persistent host + mock resolver; a new *action* still <30 lines of registry + tests.
- Zero wrong-tenant writes possible in fleet mode (enforced by `F-MT-4`).

## 16. Open decisions (need an owner before the dependent milestone)

1. **Persistent PowerShell host design (`F-PLAT-1`, blocks M7+):** single multiplexed host vs one host per module family; crash/restart + re-auth strategy; memory bounding (EXO sessions are heavy). *Recommendation: one host per module family, lazily started, health-checked, auto-reconnect using the connect hint the provider already declares.*
2. **Tenant switching model (`F-MT-2`, blocks M8):** serial teardown (safe, honours single-active-tenant) vs warm token pool (fast, multiple live tenants). *Recommendation: serial default; warm pool an opt-in flag with small TTL and an on-screen "N tenants warm" indicator.*
3. **Extensibility trust model (`F-PLAT-8`, blocks M10):** are third-party *code* plugins ever allowed, or declarative-only forever? *Recommendation: signed declarative packs are supported; code plugins are default-deny, opt-in per-path, never "Graphdeck-verified".*
4. **Audit chain segmentation vs retention (`F-COMP-6`+`F-COMP-7`):** how to prune old entries while keeping the chain verifiable — segment boundaries anchored externally before pruning. Decide the segment format before writing more chain-dependent tooling.
5. **Per-tenant vs global config for policy/standards:** per-profile, global, or layered? *Recommendation: layered (global default ← profile override), same precedence as provider preferences.*

---

## Appendix A — Provenance

v2.0 was produced by auditing the shipped code against v1.0's docs and running a structured multi-lens ideation (workload coverage, MSP/multi-tenant, security/IR, compliance/change-management, power-user UX, platform engineering, competitive analysis vs CIPP/AdminDroid/M365DSC/Maester/ORCA/k9s) followed by two adversarial critique passes that deduplicated overlapping proposals, corrected ~20 provider/feasibility claims (§2.3), re-ranked priorities against dependency reality, and surfaced categories no single lens proposed (distribution lists, helpdesk mailbox pack, BitLocker/LAPS, email-auth posture, joiner flow, Defender XDR, group-based-licensing errors, unified recovery hub, licence cost optimisation, sovereign clouds, migration workbench, shift-handover digest, as-built documentation, AI-assist boundary, screen-reader programme, leaver data-handoff, change-plan collaboration). 134 raw ideas were reduced to the deduplicated catalog above.

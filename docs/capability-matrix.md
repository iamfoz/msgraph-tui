# Provider capability matrix

This matrix answers, per task area: can it be done via Graph REST, an official
Graph SDK, Graph PowerShell, or a workload PowerShell module — and which
provider is preferred, most complete, safest, easiest to test, supports
app-only vs delegated auth, supports rollback/change validation, is currently
implemented, requires beta endpoints, and has known limitations.

Graphdeck is deliberately honest here: **Graph does not have feature parity
across all Microsoft 365 workloads.** Where a workload PowerShell module is
the only practical supported path, that module is the preferred provider —
PowerShell is a first-class backend, not a legacy fallback.

Legend: ✅ full support · ⚠️ partial · ❌ not practical · 🔶 stub/designed

## Identity (Entra ID)

| Question | Users CRUD | Groups & membership | Directory roles | Apps / service principals |
|---|---|---|---|---|
| Graph REST? | ✅ | ✅ | ✅ | ✅ |
| Official SDK? | ✅ | ✅ | ✅ | ✅ |
| Graph PowerShell? | ✅ | ✅ | ✅ | ✅ |
| Workload PS module? | ❌ (n/a) | ⚠️ Exchange PS for DLs only | ❌ | ❌ |
| Preferred | graph_rest | graph_rest | graph_rest | graph_rest |
| Most complete | graph_rest (v1.0+beta) | graph_rest; Exchange PS for DL specifics | graph_rest | graph_rest |
| Safest | graph_rest (typed templates, ETag capture) | graph_rest | graph_rest (+ typed confirmation in Graphdeck) | graph_rest |
| Easiest to test | mock, then graph_rest (MockTransport) | same | same | same |
| App-only auth | ✅ | ✅ | ✅ | ✅ |
| Delegated auth | ✅ | ✅ | ✅ | ✅ |
| Rollback / validation | ✅ full (property restore, membership compensating change) | ✅ full | ⚠️ compensating change w/ strict checks | ⚠️ metadata only; credential changes not reversible |
| Implemented in MVP | ✅ graph_rest + graph_powershell templates + mock | ✅ same | 🔶 designed | 🔶 designed |
| Beta needed | ❌ | ❌ | ❌ (PIM: ⚠️ some beta) | ❌ |
| Known limitations | `$search` needs ConsistencyLevel header; some props not selectable | role-assignable groups need RoleManagement scopes | PIM-managed roles need PIM APIs | secret *values* never retrievable (by design) |

## Licensing

| Question | Subscribed SKUs / assignment |
|---|---|
| Graph REST? | ✅ (`/subscribedSkus`, `assignLicense`) |
| SDK / Graph PS? | ✅ / ✅ |
| Workload PS? | ❌ (MSOnline retired) |
| Preferred / most complete | graph_rest |
| Rollback | ✅ full for direct assignment; ⚠️ group-based licensing changes are indirect and flagged |
| Implemented | ✅ (mock + graph_rest + Graph PS template for SKUs) |
| Limitations | usage location must be set before assignment; group-based licensing conflicts surfaced as warnings |

## Exchange Online

| Question | Mailbox permissions / forwarding / holds / quotas / shared mailboxes |
|---|---|
| Graph REST? | ⚠️ mailbox *settings* (auto-reply, some forwarding via settings) only; ❌ Add-MailboxPermission equivalents |
| Official SDK? | ⚠️ same gaps as REST |
| Graph PowerShell? | ⚠️ same gaps |
| Workload PS? | ✅ ExchangeOnlineManagement (Get/Set-Mailbox, *-MailboxPermission, transport) |
| Preferred | **exchange_powershell** |
| Safest | exchange_powershell with `-WhatIf` + Graphdeck snapshot |
| App-only | ✅ (certificate-based app-only supported by the module) |
| Rollback | ✅ full via before-state capture of forwarding values (set_forwarding); permission-change rollback designed |
| Implemented | ✅ **Exchange module shipped**: list/get mailboxes, mailbox permissions, inbox rules (BEC hunt), and set/clear forwarding (HIGH-risk, typed confirm, FULL rollback) — over the persistent PowerShell host in live mode, and the mock engine offline. Retention/quotas/transport still designed. |
| Limitations | module ~1 GB memory in long sessions; live validation needs a tenant + pwsh (the persistent host reuses one Connect-ExchangeOnline across commands) |

## Teams

Graph REST covers teams/channels/membership reads and many writes ✅; policy
management (meeting/messaging/calling policies, policy assignment) is
MicrosoftTeams PowerShell ✅ → preferred provider is **graph_rest for
inventory/membership, teams_powershell for policies**. Implemented: 🔶 designed.

## SharePoint / OneDrive

Graph REST covers sites/drives/permissions reads well ✅; tenant-level admin
settings (sharing caps, site properties) are SPO PowerShell ✅ (or PnP,
non-Microsoft-supported — not used by default). Preferred: graph_rest for
sites inventory, **sharepoint_powershell for tenant settings**. Implemented:
🔶 designed.

## Intune / Devices

Graph REST (`/deviceManagement`) is the *only* interface ✅ — there is no
supported standalone Intune PowerShell module (the old one is deprecated;
Microsoft.Graph covers it). Preferred: graph_rest. Some reports need beta ⚠️.
Implemented: 🔶 designed (read-only first).

## Security & Compliance (Purview)

eDiscovery ⚠️ some Graph beta; retention/DLP policy management ❌ Graph —
**Security & Compliance PowerShell is required** ✅ (Connect-IPPSSession).
App-only support ⚠️ limited. Rollback: ⚠️ manual-guidance for most policy
objects. Implemented: 🔶 designed.

## Audit logs / sign-ins

Graph REST ✅ (`/auditLogs/directoryAudits`, `/auditLogs/signIns` — needs
Entra ID P1 for sign-ins ⚠️); Unified Audit Log search is SCC PowerShell
(`Search-UnifiedAuditLog`) ✅. Preferred: graph_rest for directory audit,
scc_powershell for UAL. Implemented: 🔶 designed (Graphdeck's own local audit
trail is ✅ implemented).

## Engine status summary

| Engine | Status |
|---|---|
| mock | ✅ full (all MVP actions, mutation, error simulation) |
| graph_rest | ✅ implemented (auth, pagination, throttling, normalisation) |
| powershell (graph/exchange/teams/spo) | ✅ engine implemented (builder, parser, module detection); workload actions land per milestone |
| graph_sdk | 🔶 interface-complete stub; REST provides parity meanwhile |

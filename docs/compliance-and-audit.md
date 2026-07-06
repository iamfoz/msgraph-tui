# Compliance & audit guide

> **Disclaimer — read first.** ISO/IEC 27001 conformity depends on your
> organisation's ISMS: its policies, controls, scope, risk treatment and
> implementation. Graphdeck **does not make you compliant** and does not claim
> certification of any kind. It provides supporting technical controls,
> tamper-evident records and export tooling that can serve as *evidence*
> within your own ISMS and change-management process.

## What Graphdeck records

Every action initiated through Graphdeck produces an audit event containing:

- **Who**: signed-in actor and authentication mode; interaction mode
  (interactive / bulk / scripted / imported)
- **When**: UTC timestamp
- **Where**: tenant ID (and name where known)
- **What**: action ID, provider used, object ID/type, the exact redacted
  Graph request / SDK call / PowerShell command
- **State**: before-state and after-state where safely retrievable
- **Why**: reason, ticket/change reference, requestor, expiry, approval
  reference, notes (when the change-reason policy is enabled — on by default)
- **Outcome**: success/failure, normalised errors, warnings, retry/throttling
  info, correlation IDs, duration
- **Reversibility**: rollback snapshot linkage; rollback events reference the
  original operation ID
- **Risk**: the action's risk classification
- Dry-run mode records `change_intent` events — evidence of planned changes.

Reads are logged as `read` events by default (`log_reads_to_audit`,
configurable).

## Tamper evidence

The audit log is append-only JSONL. Each entry stores:

```
prev_hash  = entry_hash of the previous record (genesis: 64×"0")
entry_hash = SHA-256( prev_hash + canonical_json(record) )
```

Any edit, insertion or deletion breaks the chain from that point. Verify from
the Session screen, the Audit view (`v`), or in code
(`AuditLog.verify()` — also executed inside every evidence pack). The chain is
tamper-evident, not tamper-proof: export evidence packs (or forward the JSONL
to your SIEM) regularly so an independent copy exists.

**Secrets are never stored.** Events pass through the redaction service before
hashing; tests assert that passwords, tokens and auth headers cannot reach the
file.

## Change-reason policy

`require_change_reason` (default **on**) makes every write collect a reason
and optional ticket reference before the confirmation gate; the executor
refuses commits without one. Set it to `false` only if your change process
records context elsewhere.

## Evidence packs

*Session & providers → Export evidence pack* (or
`compliance.evidence.generate_evidence_pack`) writes a directory containing:

| File | Contents |
|---|---|
| `manifest.json` | tool version, period, counts, chain verification result, SHA-256 of every file, disclaimer |
| `change-history.jsonl` / `.csv` | all change/rollback/intent events (CSV flattened for auditors) |
| `high-risk-actions.jsonl` | risk ∈ {high, destructive} |
| `failed-changes.jsonl` | changes whose result was failure |
| `rollbacks.jsonl` | rollback events with original-operation linkage |
| `approval-references.json` | ticket/approval metadata per operation |
| `administrator-activity.json` | events per actor |
| `integrity-verification.json` | chain verification output |

## Mapping to common ISO 27001:2022 Annex A themes

*(Indicative, not a compliance claim — your Statement of Applicability governs.)*

| Annex A control theme | Graphdeck support |
|---|---|
| 5.16 / 5.18 Identity & access rights | role/membership/licence views + exports for access reviews |
| 8.2 Privileged access rights | privileged-action tagging, typed confirmation, role-assignable group warnings |
| 8.9 Configuration management | before/after capture, change records, drift-checked rollback |
| 8.15 Logging | append-only hash-chained log, separation of audit vs debug, retention config |
| 8.16 Monitoring activities | provider request history, error history, throttling records |
| 8.19 / change management | reason+ticket policy, preview gate, approval references, evidence packs |
| 5.28 Collection of evidence | evidence pack with manifest and file hashes |

## Retention & data handling

- `audit_retention_days` (default 365) and `debug_log_retention_days`
  (default 30) are configuration knobs; enforcement of retention is the
  operator's scheduled task in the MVP (documented limitation).
- All records are local; nothing is transmitted anywhere except the Microsoft
  endpoints invoked and any log sink *you* configure.
- Users can purge all local state by deleting the documented directories.

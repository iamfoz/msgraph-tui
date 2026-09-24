# Graphdeck security model

## Principles

1. **No secret persistence.** Passwords are never handled at all (device-code
   and certificate flows only). Access tokens live in process memory and die
   with the process. Nothing writes tokens, refresh tokens, client secrets,
   private keys or certificate material to disk.
2. **Redaction at every boundary.** A single service (`core/redaction.py`)
   scrubs sensitive key names (password, secret, token, authorization,
   credential, private key, …) and secret-shaped patterns (JWTs,
   `Bearer`/`Basic` values, `client_secret=`/`password=` pairs, PEM blocks)
   from previews, debug logs, audit events, rollback snapshots and exports.
   Safe metadata (expiry dates, thumbprints, hints) is deliberately preserved.
   Enforced by tests (`test_redaction.py`, `test_audit.py::test_secrets_never_stored`).
3. **Transparency over trust.** The exact Graph request or PowerShell command
   is shown before execution — the preview is a core feature, and what is
   previewed is what runs (same builder produces both).
4. **Tenant context is structural.** One tenant per session; tenant, account
   and mode are permanently displayed in the status bar and repeated inside
   every confirmation dialog. Cross-tenant mistakes require ignoring three
   separate indicators.
5. **Risk-scaled friction.** Read-only actions are frictionless. Writes need
   confirmation + (by default policy) a reason and ticket. High-risk,
   destructive, privileged and bulk actions require typing a confirmation
   phrase. Role-assignable groups get extra warnings.
6. **No arbitrary execution.** Command and parameter *names* come only from
   the registry; user input is bound as values with strict quoting. There is
   no raw PowerShell/Graph console in the MVP; if one is added it will be
   opt-in and clearly labelled.
7. **Least privilege, explained.** Every action declares and displays its
   required Graph scopes and typical admin roles, so operators can request
   the minimum. Graphdeck never elevates, never consents to new permissions
   silently, and asks before anything that would trigger a consent prompt.
8. **No exfiltration.** No telemetry, no phone-home, no external services.
   Tenant data goes only to the Microsoft endpoints the operator invokes.
   Exports are deliberate user actions written to a documented local
   directory. All local state is user-purgeable.

## Injection resistance

- **Graph paths**: template segments are URL-quoted per segment
  (`a b/c?@x` → `a%20b%2Fc%3F%40x`); user input can fill declared slots but
  cannot alter the request shape.
- **PowerShell**: values are emitted exclusively as single-quoted PowerShell
  literals with embedded quotes doubled — inside single quotes PowerShell
  performs no expansion of variables, subexpressions or backticks. Command,
  parameter and property names are validated against
  `^[A-Za-z][A-Za-z0-9_-]*$`. Execution uses `asyncio.create_subprocess_exec`
  (no shell), `-NoProfile -NonInteractive`. Hostile inputs are covered by
  `test_ps_builder.py::test_injection_attempts_are_inert`.

## Threats considered

| Threat | Mitigation |
|---|---|
| Token theft from disk | Tokens never persisted. |
| Secret leakage via logs/audit/exports | Central redaction, tested; audit and debug streams separated. |
| Command injection via object names (a group named `'; Remove-Item …`) | Quoting/identifier validation as above. |
| Wrong-tenant change | Single tenant context, persistent display, tenant repeated in confirmation. |
| Unnoticed high-impact change | Risk classification, typed confirmation, before/after capture, audit chain. |
| Local audit tampering | Hash chain + out-of-band **head anchor** (detects tail truncation), **advisory locking** (safe concurrent append), **optional HMAC keying** (forgery-resistant with an operator key), verification command, and optional off-host copies. |
| Endpoint tampering (token exfiltration) | `graph_base`/`authority` validated on load — non-HTTPS is rejected and non-Microsoft hosts warn loudly before any token is sent. |
| Malicious rollback (replaying stale state) | Drift detection blocks rollback when the object changed since the operation; blast-radius check; snapshots single-use. |
| Single admin making a high-impact change unilaterally | Optional four-eyes gate in the Executor: the change request is bound to a content hash of action + parameters + tenant, the approver must be a different identity from the requester and the executor, bulk runs need one approval covering every object, applied requests cannot be replayed, and every decision is audited. |
| Over-privileged app registration | Per-action scope display encourages minimal grants; default scope set is read-only. |

## Residual risks / honest limitations

- Graphdeck runs with the operator's privileges; it cannot protect against a
  fully compromised workstation.
- Audit integrity is layered. The hash chain catches in-place edits and
  reordering; the head anchor (`audit.jsonl.head`) additionally catches tail
  truncation and out-of-band appends. **Without an HMAC key** the log remains
  tamper-*evident*, not tamper-*proof*: an attacker who can rewrite both the log
  and its head file could rebuild a consistent chain. Configure
  `audit_hmac_key_path` (a key the operator controls, stored apart from the
  log) to make the chain unforgeable without that key, and/or forward copies
  (evidence packs, SIEM export) to an independent system, for stronger
  guarantees.
- Redaction covers key-name matches, `key=value`/dict-repr'd secrets, JWTs,
  Bearer/Basic values, PEM blocks, storage `AccountKey`/SAS `sig`, and secret
  values rendered into command strings (e.g. a PowerShell `-Password 'x'`).
  It is deny-list + pattern based; a genuinely novel secret shape in free text
  could still slip through, so treat exports as sensitive regardless.
- Four-eyes strength depends on configuration:
  - **No keys:** identities are the names people type (`--as bob`) plus the
    signed-in account. It's a process control that makes bypassing review
    deliberate and visible in the audit trail.
  - **Shared HMAC key** (`approval_signing_key_path`): approvals are
    tamper-evident, but the signature proves only that *someone* holding the
    key approved, not who.
  - **Personal Ed25519 keys + trust store** (`approver_trust_store_path`): an
    approval verifies only against a public key registered to the named
    approver. Forging Bob's approval needs Bob's private key (0600,
    optionally passphrase-encrypted, never copied into Graphdeck state).
  - **What remains trusted:** the trust store itself. Anyone who can add a key
    to it can approve, so keep it under change control that requesters can't
    edit. Identity in the trust store is whatever the maintainer wrote, not an
    Entra ID check. A requester with admin rights in the tenant can still make
    the change outside Graphdeck: four-eyes governs this tool, not the tenant.
  - The approver signs the request's *content hash*. Graphdeck re-derives that
    hash from the request's parameters before showing or signing it, so a
    request file doctored to look harmless is refused.
  - Signed approvals from outside the workstation (import, git) are always
    verified; unsigned ones are refused.
  - The git channel adds transport, not trust. Push access to the approvals
    repo cannot approve anything when a trust store is configured.
  - Webhook URLs embed tokens: only the host is logged, and notifications
    carry no parameters or previews.
  - Rollbacks are deliberately exempt from the gate.
- PowerShell module output is parsed defensively but modules execute with
  full user privileges — install modules only from trusted sources
  (Graphdeck never installs them for you).

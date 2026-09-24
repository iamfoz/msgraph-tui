# Graphdeck user guide

## Installation

Requirements:

- **Python 3.11+**
- A terminal with 256-colour support (any modern terminal, including over SSH)
- For live PowerShell-backed actions: **PowerShell 7 (`pwsh`)** — Windows
  PowerShell 5.1 is also detected on Windows
- For live Graph actions: the optional `msal` dependency and an Entra ID app
  registration (see below)

```bash
# from a clone of this repository
pip install .            # core (mock + dry-run modes work immediately)
pip install '.[live]'    # adds msal for live Microsoft Graph authentication
pip install '.[signing]' # adds cryptography for personal (Ed25519) approver keys
pip install '.[dev]'     # adds pytest for running the test suite
```

`pipx install .` or `uv tool install .` work equally well and keep Graphdeck
isolated.

## Running

```bash
graphdeck --mock      # fixture tenant, no network, no PowerShell — start here
graphdeck --dry-run   # full pipeline incl. previews & audit-of-intent; never executes
graphdeck --live      # real tenant (requires configuration below)
graphdeck --fixtures ./my-fixtures   # point mock mode at custom fixture JSON
python -m msgraph_tui --mock         # equivalent without the entry point
```

The active mode is always visible in the status bar and echoed in every
confirmation dialog.

### Mock mode

Mock mode hydrates an in-memory tenant from bundled JSON fixtures (12 users,
6 groups, 4 SKUs). Everything works: browsing, searching, exports, **write
actions actually mutate the mock tenant**, audit events are recorded, rollback
snapshots are captured and can be executed. Use it for demos, training,
screenshots and development. Add `_simulate` values via custom fixtures to
rehearse failure handling.

### Dry-run mode

Dry-run uses mock data for reads, and for writes runs the entire safety
pipeline — provider resolution, request preview, before-state capture, reason
collection, confirmation — then records a `change_intent` audit event **without
executing anything**. Use it to rehearse a change plan and produce evidence of
intended changes.

### Live mode

1. Create (or reuse) an Entra ID app registration with **delegated** Graph
   permissions you intend to use (start with `User.Read.All`,
   `Group.Read.All`, `Organization.Read.All`) and enable
   *Allow public client flows* for device-code sign-in.
2. Configure Graphdeck:

```jsonc
// ~/.config/msgraph-tui/config.json
{
  "mode": "live",
  "tenant_id": "<your-tenant-guid>",
  "client_id": "<app-registration-client-id>",
  "allow_beta": false,
  "require_change_reason": true,
  "provider_preferences": {
    "Exchange Online": "exchange_powershell"   // per-service override example
  }
}
```

3. `graphdeck --live`, open **Session & providers**, and sign in with the
   device code shown. Tokens are held in memory only and never written to disk.

Environment overrides: `GRAPHDECK_MODE`, `GRAPHDECK_TENANT_ID`,
`GRAPHDECK_CLIENT_ID`, `GRAPHDECK_ALLOW_BETA`, `GRAPHDECK_CONFIG_DIR`,
`GRAPHDECK_STATE_DIR`, `GRAPHDECK_AUTH_MODE` (`delegated` or `app-only`),
`GRAPHDECK_REQUIRE_APPROVAL` (comma-separated risk levels, e.g.
`high,destructive`).

## Where Graphdeck keeps data

| Path | Contents |
|---|---|
| `~/.config/msgraph-tui/config.json` | configuration |
| `~/.local/share/msgraph-tui/audit/audit.jsonl` | hash-chained audit log (append-only) |
| `~/.local/share/msgraph-tui/snapshots/` | rollback snapshots (one JSON per change) |
| `~/.local/share/msgraph-tui/exports/` | exports and evidence packs |
| `~/.local/share/msgraph-tui/logs/debug.log` | redacted debug log (separate from audit) |

Deleting these directories clears all local cache and logs; nothing else is
stored anywhere, and nothing is transmitted anywhere except the Microsoft
endpoints you explicitly invoke.

## Keyboard model

- `Ctrl+P` — command palette: jump to any screen, launch any admin action, or
  switch the colour theme (dark/light/high-contrast and more)
- `F1` / `?` — help and safety-model summary
- `Ctrl+L` — toggle the logs panel
- `/` — focus the filter box in any table (browse **and** audit/change views);
  `Esc`/`Enter` returns to the table
- `Enter` — open detail for the selected row
- `s` — cycle sort column (asc → desc → next column → unsorted)
- `g` / `G` — jump to top / bottom
- `y` / `Y` — copy the current row to the clipboard as JSON / CSV (redacted)
- `r` refresh · `p` provider/request preview · `Ctrl+E` export
- Row operations are listed under each table (e.g. in Users: `l` licences,
  `m` memberships, `u` update, `d` enable/disable, `a`/`x` assign/remove
  licence, `k` revoke sessions)

## Command-line subcommands

Beyond launching the TUI, `graphdeck` exposes headless subcommands that reuse
the same audit/redaction services (exit codes make them scriptable):

```bash
graphdeck verify-audit                     # verify the hash chain + head anchor (exit 0/1)
graphdeck evidence-pack --zip              # export an evidence pack (period: --since/--until)
graphdeck actions --json                   # machine-readable action catalog (registry as data)
graphdeck purge --logs --yes               # delete debug logs
graphdeck purge --all --yes                # logs + exports + audit (audit is evidence: needs --yes)
graphdeck approvals [--all]                # list four-eyes change requests (pending by default)
graphdeck approve <id> --as <name>         # approve as a different person (--reject, --comment)
graphdeck apply <id>                       # execute an approved request through the full pipeline
```

## Four-eyes approval (optional)

Turn it on per risk level, in the config file or the environment:

```json
{ "require_approval_for_risk": ["high", "destructive"],
  "approval_signing_key_path": "/secure/approval.key" }
```

When a gated change reaches the confirmation screen, a banner says approval is
needed. Confirming **submits a change request** instead of executing; nothing
changes in the tenant. A different person reviews and decides:

```bash
graphdeck approvals                        # what's waiting
graphdeck approve 3f9c1a2b7d4e --as bob@contoso.example --comment "checked with HR"
```

The requester then either re-runs the same change in the TUI (the banner now
shows who approved it and confirming executes) or runs `graphdeck apply <id>`.

Rules enforced by the Executor:

- The approval covers **that exact change**: same action, parameters and
  tenant. Changing any value, or targeting another object, needs a new request.
- The approver cannot be the requester or the person executing.
- A bulk run needs one approval that covers every selected object.
- An applied request cannot be used again. Rejected requests cannot be applied.
- Rollbacks are never gated, so an urgent undo is not held up.
- Changes whose parameters contain secrets cannot be queued; the request file
  would store them.
- Dry-run mode is not gated because nothing executes.

Requests and approvals are JSON files under `<state>/approvals/`; every
request, decision and execution is written to the audit log.

### Personal approver keys (recommended)

Without keys, an approval is just a name someone typed. With personal
Ed25519 keys, an approval only counts if it was signed by a key registered to
that approver, so nobody can approve in a colleague's name. Needs
`pip install '.[signing]'`.

1. Each approver creates a key on their own machine:
   `graphdeck keygen --as bob@contoso.example`. It asks for a passphrase and
   writes the private key with owner-only permissions. It prints the public key
   and a snippet for the trust store. The private key never leaves that machine.
2. Whoever owns the process keeps a **trust store** file listing approvers and
   their public keys:
   `{"approvers": {"bob@contoso.example": ["<public key>"]}}`. Store it where
   requesters can't edit it, because anyone who can add a key to it can approve.
3. Everyone sets `approver_trust_store_path` to that file. Approvers also set
   `approver_key_path` (or `GRAPHDECK_APPROVER_KEY`) to their own key, or pass
   `--key`. For scripts, `GRAPHDECK_APPROVER_KEY_PASSPHRASE` avoids the prompt.

Once a trust store is configured, approvals that are unsigned, HMAC-signed,
signed by an unregistered key, or edited after signing are all refused.
`approval_signing_key_path` (one shared HMAC key) still works when no trust
store is set, but it only proves that *someone* holding the key approved.

### Approving from another machine

The approver doesn't need tenant access or a copy of your state directory:

```bash
graphdeck approval-export 3f9c1a2b7d4e --out req.json          # requester
graphdeck approve --request-file req.json --as bob@contoso.example --out approval.json   # approver
graphdeck approval-import approval.json                          # requester
graphdeck apply 3f9c1a2b7d4e
```

Send the files any way you like (email, chat, ticket). The approver sees the
exact parameters the approval covers. A request file edited to show different
parameters from the ones its hash covers is refused before anything is signed.
Only signed approvals can be imported, and each one is checked against the
trust store (or HMAC key) and against the request it claims to approve.

### Reviewing requests as git pull requests

Point Graphdeck at a local clone of a shared repository:

```json
{ "approval_channel": "git", "approval_git_repo": "~/src/m365-approvals",
  "approval_git_remote": "origin", "approval_git_base_branch": "main" }
```

- Submitting a request pushes a branch `graphdeck/cr-<id>` containing
  `change-requests/<id>/request.json`. Graphdeck prints a link for opening a
  PR, so the change can be discussed and reviewed like code.
- `graphdeck approve <id> --as <name>` fetches the request from the repo if it
  isn't local and pushes the signed decision (`approval.json`) to the same
  branch.
- `graphdeck apply <id>` and `graphdeck approvals --sync` pull decisions from
  the repo and verify them before accepting them.
- Graphdeck only uses git plumbing and never checks out branches or touches
  your working tree. It commits with the clone's own git identity.
- If the push fails (e.g. offline), the request is still saved locally and you
  get a warning. Use the export/import commands above as a fallback.

Being able to push to the repo is not enough to approve: when a trust store is
configured, the decision must still carry a valid personal signature.

### Webhook notifications

Set `approval_webhook_url` (or `GRAPHDECK_APPROVAL_WEBHOOK_URL`) to a Teams,
Slack or other incoming webhook. It is notified when a request is submitted,
approved, rejected, or applied. Messages carry the request id, action, risk,
requester, reason, and the commands to run. They leave out parameters and
previews, which can contain personal data. The URL must be https. It usually
embeds a token, so treat it as a secret: only its host is ever logged. A
failed notification never blocks a request.

## Tamper-proofing the audit log (optional)

By default the audit chain is tamper-*evident*. To make it unforgeable without
an operator-held key, point `audit_hmac_key_path` at a key file (stored apart
from the log). Entries are then HMAC-signed; `graphdeck verify-audit` needs the
same key to verify. See `docs/security-model.md`.

## Making a change (what to expect)

1. Pick a row operation or form action.
2. Fill the guided form (pre-filled from the selected object).
3. The **confirmation gate** shows: the exact Graph request or PowerShell
   command, why that provider was selected, required scopes/roles, risk badge,
   rollback support, and the captured before-state.
4. Enter a change reason and ticket reference (required by default policy).
5. High-risk actions additionally require typing a confirmation phrase.
6. On execution: after-state is captured, a hash-chained audit event is
   written, and rollback availability is shown.

## Rolling back

Open **Compliance → Change history & rollback**, select a change, press `b`.
Graphdeck re-reads the object, runs sanity checks (does it still exist? has
anyone changed it since?), shows a before/current diff and the exact inverse
operation, and only then lets you confirm. If the object drifted, automatic
rollback is **blocked** — that is by design; re-apply the desired state as a
new change instead. Rollbacks execute as new audited changes linked to the
original operation.

## Audit & evidence

- **Compliance → Audit log**: every event; `v` verifies the hash chain.
- **Session & providers → Export evidence pack**: writes a directory with
  change history (JSONL + CSV), high-risk register, failures, rollbacks,
  approval references, administrator activity, the chain verification result
  and a manifest with SHA-256 file hashes.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Not signed in to Microsoft Graph` | Open Session screen and sign in; check `tenant_id`/`client_id` in config. |
| `AADSTS70011 / invalid scope` | The app registration lacks the permission; grant it and re-consent. |
| `Authorization_RequestDenied` | Signed-in account lacks the admin role / scope shown in the action preview. |
| `TooManyRequests` warnings | Microsoft throttling; Graphdeck honours `Retry-After` automatically — narrow the query if persistent. |
| `The term 'Get-MgUser' is not recognized` | PowerShell module missing — Session → *Check PowerShell modules* shows exact `Install-Module` guidance (Graphdeck never installs automatically). |
| `Connect-MgGraph`/`Connect-ExchangeOnline` in errors | The PowerShell session expired; reconnect using the hint in the error panel. |
| Non-JSON PowerShell output errors | A module wrote noise around the JSON; Graphdeck tolerates leading/trailing noise — see the raw output view for what arrived. |
| `AUDIT CHAIN BROKEN` | The local audit file was edited or truncated. Preserve the file for investigation; the first divergent entry number is reported. |

## Recent additions

- **App-only (certificate) auth** for unattended/headless runs: set
  `auth_mode: "app-only"`, `client_certificate_path`, and
  `client_certificate_thumbprint` (plus tenant/client id) in the config and
  live mode authenticates automatically — no interactive sign-in. Or switch at
  runtime: **Session & providers → Use app-only (certificate)**. The button
  requests a token straight away, so a bad certificate or missing consent shows
  up immediately, and it leaves your current sign-in in place if that fails.
- **Bulk multi-select**: `Space` to select rows (`Ctrl+A` all, `Ctrl+D` clear),
  then a write key applies to the whole selection through a bulk wizard with a
  typed `VERB n` confirmation. Each object gets its own audited change and
  rollback snapshot.
- **Exchange Online module** (Exchange → Mailbox list): mailboxes, mailbox
  permissions, inbox rules (a BEC/forwarding hunt surface), and set/clear
  forwarding (high-risk, typed confirm, full rollback). Runs on the persistent
  PowerShell host in live mode (one `Connect-ExchangeOnline` reused across
  commands) and on fixtures offline. Live Exchange needs `pwsh` +
  `ExchangeOnlineManagement` + a tenant.
- **Teams** (meeting and messaging policies, per-user lookup, grant a meeting
  policy with full rollback), **SharePoint** (site list/detail, set a site's
  sharing capability: high risk, typed confirm, full rollback; sites allowing
  guest sharing are flagged) and **Compliance → Retention / DLP policies**
  (read-only; DLP policies still in test mode are flagged). Live use needs
  `pwsh` plus `MicrosoftTeams`, `Microsoft.Online.SharePoint.PowerShell` or
  `ExchangeOnlineManagement` (for `Connect-IPPSSession`).
- **Four-eyes approval**: see the section above.

## Known limitations

- Live coverage today: Users / Groups / Licences via Graph REST; Exchange,
  Teams policies, SharePoint sites and Purview retention/DLP (read-only) via
  workload PowerShell. Intune, Roles and Apps modules remain designed (see
  capability matrix).
- Graph SDK engine is a stub (REST provides the same coverage).
- Four-eyes identities are only as strong as your configuration: without a
  trust store they are typed names. Graphdeck doesn't open the pull request
  for you (it pushes the branch and prints the link), and it can't stop anyone
  merging or closing that PR; the PR is for discussion, and the signed
  `approval.json` is what counts.
- Server-side `$search`/`$filter` for large tenants is not wired; table
  filtering is client-side over the fetched page set.
- Workload PowerShell (Exchange, Teams, SharePoint, Purview) has been validated against fixtures and a
  fake host transport, not a live tenant — the persistent host's real `pwsh`
  behaviour still needs tenant validation.

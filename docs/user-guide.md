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
`GRAPHDECK_STATE_DIR`.

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
```

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

## Known limitations

- Live coverage is Users / Groups / Licences via Graph REST; Exchange, Teams,
  SharePoint, Intune, Roles and Apps modules are designed (see capability
  matrix) but not yet wired to screens.
- Graph SDK engine is a stub (REST provides the same coverage).
- App-only (client-credentials) auth is not wired yet — sign-in is delegated
  device-code only.
- No four-eyes approval workflow yet (approval references *can* be recorded in
  the change-reason fields and appear in evidence packs).
- Bulk multi-select operations are not yet exposed in the UI (single-object
  actions only).
- Server-side `$search`/`$filter` for large tenants is not wired; table
  filtering is client-side over the fetched page set.

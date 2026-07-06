# Rollback guide

Rollback in Graphdeck is a **controlled compensating change** — a new, fully
audited operation that restores previously captured state after strict sanity
checks. It is never a "magical undo": if reality moved on since your change,
Graphdeck refuses to blindly overwrite it.

## Snapshot capture (automatic, before every eligible write)

When you commit a write whose action declares rollback support, Graphdeck:

1. Runs the action's paired **read action** to capture the before-state of the
   tracked fields (e.g. `users.get` before `users.update`; `groups.members`
   before a membership change).
2. Builds the **inverse operation** (action + params) and its preview.
3. Stores a snapshot: tenant, object ID/type, provider, operation ID,
   timestamp, actor, before-state, original request, inverse request,
   concurrency marker (ETag where available), tracked fields, non-restorable
   fields, dependencies, risk, required checks.
4. After successful execution, captures the **after-state** into the same
   snapshot (this is what makes third-party drift detectable later).

If before-state capture fails, **no rollback is offered** — the confirmation
dialog warns you before you execute.

## Support levels

| Level | Meaning |
|---|---|
| `none` | Not reversible (e.g. revoking sessions). High-risk actions must document why. |
| `manual_guidance` | Graphdeck explains reversal steps but will not automate them. |
| `partial` | Some fields restorable; non-restorable fields are listed. |
| `full` | A safe inverse operation can be generated and executed. |
| `snapshot_restore` | A captured configuration document can be re-applied. |
| `recreate` | A deleted object can be recreated **only** where Microsoft APIs support it safely and all data was captured. |

## Sanity checks (all run before any rollback executes)

1. Snapshot not already consumed (rollbacks are single-use).
2. Rollback is supported for this change.
3. **Object still exists** — otherwise blocked.
4. **Object type unchanged** — otherwise blocked.
5. **No drift**: for each tracked field, the current value is compared with
   the after-state captured when the change ran. Any difference means another
   actor touched the object since → **automatic rollback is blocked** and the
   diff is displayed. Re-apply the desired state as a new change instead.
6. Non-restorable fields are listed as warnings.
7. **Blast radius**: a rollback may never affect more objects than the
   original change.
8. Destructive rollbacks and approval requirements are flagged.
9. Permissions and provider/API validity are re-evaluated by executing the
   rollback through the normal write pipeline (which re-previews, re-captures
   state and re-audits).

## The rollback UI shows

Original change summary · before-state · current state · proposed rollback
state · field-level diff · sanity-check results · risk warnings ·
dependencies · the provider and **the exact inverse request/command** ·
reason collection · confirmation. The rollback is recorded as a new audit
event linked to the original operation ID, and the snapshot is marked
consumed.

## Worked examples

1. **User property update** — `users.update` captures
   `displayName/department/jobTitle/officeLocation`. Rollback restores the
   changed fields *iff* the user still exists and nobody altered those fields
   since (drift check per field).
2. **Group membership** — `groups.member_add/remove` snapshot the member ID
   list. Rollback removes the added member / re-adds the removed member after
   re-reading current membership; any intervening membership change blocks
   auto-rollback.
3. **Licence assignment** — `users.assign_license/remove_license` snapshot
   `assignedLicenses`. Rollback re-removes / re-assigns, with warnings that
   SKU availability, disabled service plans or group-based licensing may have
   changed; workload data retention after licence removal depends on the
   service and is documented as non-restorable.
4. **Mailbox permission change** *(designed, M4)* — before-state =
   `Get-MailboxPermission` output; rollback restores the previous permission
   entry only after verifying the current ACL still matches the after-state
   (protects against removing a permission someone intentionally re-added).
5. **Forwarding / mailbox configuration** *(designed, M4)* — captured
   `Get-Mailbox` values restored only if the properties are unchanged since.
6. **Role assignment** *(designed, M5)* — treated as privileged + high-risk:
   typed confirmation, mandatory current-state validation, optional approval
   reference; rollback re-checks the member list at execution time.

## When rollback is deliberately NOT offered

- Permanent deletions that Microsoft cannot fully restore.
- Actions with external side effects (mail already sent, notifications fired).
- Security changes whose reversal would reintroduce risk (e.g. re-enabling a
  compromised account is a *new* decision, not an undo — Graphdeck will let
  you do it, but as a fresh audited change).
- Bulk actions without per-object snapshots.
- Any change whose before-state was not captured or whose current state
  cannot be validated.
- Operations on undocumented/unstable APIs.

"""Headless CLI subcommands that reuse the same services as the TUI.

Each returns a process exit code and prints a short human summary. They are
pure enough to unit-test without launching the terminal UI.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import TextIO

from ..compliance.audit import AuditLog
from ..compliance.evidence import generate_evidence_pack
from ..core.config import AppConfig
from ..modules import build_registry


def verify_audit(config: AppConfig, *, out: TextIO = sys.stdout) -> int:
    """Verify the audit hash chain and head anchor. Exit 0 = intact."""
    log = AuditLog(config.audit_dir / "audit.jsonl", hmac_key=config.audit_hmac_key())
    result = log.verify()
    status = "INTACT" if result.ok else "BROKEN"
    print(f"Audit chain: {status} — {result.detail} ({result.entries} entries)", file=out)
    if not result.ok and result.first_bad_line is not None:
        print(f"  First divergence at line {result.first_bad_line}", file=out)
    return 0 if result.ok else 1


def evidence_pack(
    config: AppConfig,
    *,
    since: str | None = None,
    until: str | None = None,
    as_zip: bool = False,
    out: TextIO = sys.stdout,
) -> int:
    log = AuditLog(config.audit_dir / "audit.jsonl", hmac_key=config.audit_hmac_key())
    config.exports_dir.mkdir(parents=True, exist_ok=True)
    pack_dir = generate_evidence_pack(log, config.exports_dir, period_start=since, period_end=until)
    if as_zip:
        archive = shutil.make_archive(str(pack_dir), "zip", root_dir=pack_dir)
        shutil.rmtree(pack_dir)
        print(f"Evidence pack written: {archive}", file=out)
    else:
        print(f"Evidence pack written: {pack_dir}", file=out)
    return 0


def _human_bytes(paths: list[Path]) -> tuple[int, int]:
    files = [p for p in paths if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def purge(
    config: AppConfig,
    *,
    logs: bool = True,
    exports: bool = False,
    audit: bool = False,
    assume_yes: bool = False,
    out: TextIO = sys.stdout,
) -> int:
    """Remove local state. Debug logs by default; exports/audit are opt-in.

    The audit log and rollback snapshots are evidence, so purging them needs an
    explicit flag AND confirmation."""
    targets: list[tuple[str, list[Path]]] = []
    if logs:
        log_dir = config.debug_log_path.parent
        targets.append(("debug logs", sorted(log_dir.glob("debug.log*")) if log_dir.exists() else []))
    if exports:
        targets.append(("exports", sorted(config.exports_dir.glob("*")) if config.exports_dir.exists() else []))
    if audit:
        audit_files = sorted(config.audit_dir.glob("*")) if config.audit_dir.exists() else []
        snap_files = sorted(config.snapshots_dir.glob("*")) if config.snapshots_dir.exists() else []
        targets.append(("audit log + rollback snapshots (EVIDENCE)", audit_files + snap_files))

    all_files = [p for _label, paths in targets for p in paths]
    if not all_files:
        print("Nothing to purge.", file=out)
        return 0

    for label, paths in targets:
        n, size = _human_bytes(paths)
        print(f"  {label}: {n} files, {size} bytes", file=out)

    if audit and not assume_yes:
        print("Refusing to delete audit evidence without --yes.", file=out)
        return 2
    if not assume_yes:
        print("Re-run with --yes to confirm deletion.", file=out)
        return 2

    removed = 0
    for p in all_files:
        try:
            if p.is_file():
                p.unlink()
                removed += 1
        except OSError as exc:  # pragma: no cover - unexpected fs error
            print(f"  could not remove {p}: {exc}", file=out)
    print(f"Removed {removed} files.", file=out)
    return 0


def list_actions(*, as_json: bool = False, out: TextIO = sys.stdout) -> int:
    """Machine-readable action catalog — the registry as data."""
    registry = build_registry()
    rows = []
    for action in sorted(registry.all(), key=lambda a: a.id):
        rows.append({
            "id": action.id,
            "name": action.name,
            "service": action.service,
            "risk": action.risk.value,
            "preferred_provider": action.preferred_provider,
            "supported_providers": action.supported_providers,
            "graph_scopes": action.graph_scopes,
            "admin_roles": action.admin_roles,
            "confirmation": action.confirmation.value,
            "rollback": action.rollback.level.value,
            "write": action.is_write,
        })
    if as_json:
        print(json.dumps(rows, indent=2), file=out)
    else:
        for r in rows:
            flag = "W" if r["write"] else "R"
            print(f"  [{flag}] {r['id']:<32} {r['risk']:<11} {r['service']}", file=out)
        print(f"\n{len(rows)} actions.", file=out)
    return 0


# ---------------------------------------------------------------------------
# Four-eyes approval (F-COMP-1)
# ---------------------------------------------------------------------------

def _context(config: AppConfig):
    from .context import build_context

    return build_context(config)


def _load_approver_key(config: AppConfig, key_path: Path | None, *, out: TextIO):
    """Load this operator's Ed25519 key (explicit path, else config). None if unset."""
    import getpass
    import os

    from ..compliance.signing import load_private_key

    path = key_path or config.approver_key_path
    if path is None:
        return None
    env_pass = os.environ.get("GRAPHDECK_APPROVER_KEY_PASSPHRASE")
    try:
        return load_private_key(path, env_pass.encode() if env_pass else None)
    except ValueError as exc:
        if env_pass or "passphrase-protected" not in str(exc):
            raise
    passphrase = getpass.getpass(f"Passphrase for {path}: ")
    return load_private_key(path, passphrase.encode())


def keygen(
    config: AppConfig,
    identity: str,
    *,
    out_path: Path | None = None,
    passphrase: bool = True,
    out: TextIO = sys.stdout,
) -> int:
    """Create a personal Ed25519 approver key and print the public half."""
    import getpass
    import os

    from ..compliance.signing import generate_keypair, key_id, write_private_key
    from ..core.config import default_config_dir

    if not identity.strip():
        print("An identity is required (--as name@example.com).", file=out)
        return 2
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in identity.strip().lower())
    path = Path(out_path or default_config_dir() / "keys" / f"approver-{safe}.pem").expanduser()
    if path.exists():
        print(f"Refusing to overwrite existing key {path}.", file=out)
        return 2
    secret: bytes | None = None
    if passphrase:
        env_pass = os.environ.get("GRAPHDECK_APPROVER_KEY_PASSPHRASE")
        if env_pass:
            secret = env_pass.encode()
        else:
            first = getpass.getpass("Passphrase for the new key: ")
            if first != getpass.getpass("Repeat passphrase: "):
                print("Passphrases did not match; no key written.", file=out)
                return 2
            secret = first.encode() or None
    pem, public = generate_keypair(secret)
    write_private_key(path, pem)
    snippet = json.dumps({"approvers": {identity.strip(): [public]}}, indent=2)
    print(f"Private key written: {path} (owner-only; keep it secret, never commit it)", file=out)
    print(f"Key id: {key_id(public)}", file=out)
    print(f"Public key: {public}", file=out)
    print("Ask whoever maintains the approver trust store to add:\n" + snippet, file=out)
    print(f"Then set approver_key_path (or GRAPHDECK_APPROVER_KEY) to {path}.", file=out)
    return 0


def list_approvals(
    config: AppConfig, *, show_all: bool = False, sync: bool = False, out: TextIO = sys.stdout
) -> int:
    """List change requests (pending by default); --sync pulls decisions from git."""
    from ..compliance.approval import PENDING, ApprovalStore

    if sync:
        ex = _context(config).executor
        if ex.approval_channel() is None:
            print("No approval channel configured (approval_channel is 'file').", file=out)
        else:
            from ..compliance.channels import GitChannelError

            for r in ex.approvals.list_requests():
                if r.status != PENDING:
                    continue
                try:
                    note = ex.sync_approval(r.request_id)
                except GitChannelError as exc:
                    print(f"  sync failed: {exc}", file=out)
                    return 1
                if note:
                    print(f"  {note}", file=out)

    requests = ApprovalStore(config.approvals_dir).list_requests()
    if not show_all:
        requests = [r for r in requests if r.status == PENDING]
    if not requests:
        print("No pending change requests." if not show_all else "No change requests.", file=out)
        return 0
    for r in requests:
        scope = f"{len(r.bulk_items)} objects" if r.is_bulk else (
            str(r.params.get("user_id") or r.params.get("group_id")
                or r.params.get("mailbox_id") or r.params.get("site_url") or "")
        )
        print(f"  {r.request_id}  {r.status:<9} {r.risk:<11} {r.action_id:<30} "
              f"{scope}  by {r.requester}  ({r.created_at[:19]})", file=out)
    print(f"\n{len(requests)} request(s).", file=out)
    return 0


def _print_request(request, out: TextIO) -> None:
    print(f"Change request {request.request_id} — {request.action_name} ({request.risk})", file=out)
    print(f"  Requested by: {request.requester}   Tenant: {request.tenant_id}", file=out)
    print(f"  Reason: {request.reason.get('reason', '—')}  Ticket: {request.reason.get('ticket', '—')}", file=out)
    if request.is_bulk:
        print(f"  Bulk: {len(request.bulk_items)} objects", file=out)
        for item in request.bulk_items[:20]:
            print(f"    {json.dumps(item, sort_keys=True, default=str)}", file=out)
        if len(request.bulk_items) > 20:
            print(f"    … and {len(request.bulk_items) - 20} more", file=out)
    else:
        # Params are what the content hash covers — show them, not just the preview.
        print(f"  Parameters: {json.dumps(request.params, sort_keys=True, default=str)}", file=out)
    print("  Operation:\n    " + request.preview.replace("\n", "\n    "), file=out)


def approve(
    config: AppConfig,
    request_id: str | None,
    approver: str,
    *,
    reject: bool = False,
    comment: str = "",
    key_path: Path | None = None,
    request_file: Path | None = None,
    out_file: Path | None = None,
    out: TextIO = sys.stdout,
) -> int:
    """Record an approve/reject decision by a DIFFERENT person than the requester.

    With --request-file, signs offline (no tenant, no local store) and writes
    the approval to --out for the requester to import."""
    from ..compliance.approval import (
        PENDING,
        ChangeRequest,
        check_request_integrity,
        make_approval,
        verify_signature_only,
    )

    try:
        private_key = _load_approver_key(config, key_path, out=out)
    except (ValueError, OSError) as exc:
        print(f"Cannot load approver key: {exc}", file=out)
        return 2
    trust_store = config.approval_trust_store()
    if trust_store is not None and private_key is None:
        print("This organisation requires personal approver keys: pass --key or set "
              "approver_key_path (create one with: graphdeck keygen --as <you>).", file=out)
        return 2

    ex = None
    if request_file is not None:
        try:
            request = ChangeRequest.from_dict(json.loads(Path(request_file).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            print(f"Cannot read change request file: {exc}", file=out)
            return 1
    else:
        ex = _context(config).executor
        request = ex.approvals.load_request(request_id or "")
        if request is None and ex.approval_channel() is not None:
            from ..compliance.channels import GitChannelError

            try:
                request = ex.approval_channel().fetch_request(request_id or "")
            except GitChannelError as exc:
                print(f"Could not fetch {request_id} from the approvals repo: {exc}", file=out)
                return 1
            if request is not None:
                ex.approvals.save_request(request)
        if request is None:
            print(f"No change request {request_id!r}.", file=out)
            return 1
    if request.status != PENDING:
        print(f"Request {request.request_id} is already {request.status}.", file=out)
        return 2
    try:
        check_request_integrity(request)
    except ValueError as exc:
        print(f"Refused: {exc}", file=out)
        return 2
    _print_request(request, out)
    try:
        approval = make_approval(
            request, approver, approve=not reject, comment=comment,
            key=None if private_key else config.approval_signing_key(),
            private_key=private_key,
        )
    except ValueError as exc:
        print(f"Refused: {exc}", file=out)
        return 2
    if trust_store is not None:
        ok, why = verify_signature_only(approval, trust_store=trust_store)
        if not ok:
            print(f"Refused: {why}. Is your public key in the trust store?", file=out)
            return 2
    verdict = "REJECTED" if reject else "APPROVED"
    signed = f" (signed, {approval.algorithm} key {approval.key_id or ''})".replace(" key )", ")") \
        if approval.signature else ""

    if ex is None:  # offline signing
        payload = json.dumps(approval.to_dict(), indent=2)
        if out_file is not None:
            Path(out_file).write_text(payload + "\n", encoding="utf-8")
            print(f"{verdict} by {approval.approver}{signed}. Approval written: {out_file}", file=out)
            print(f"Send it to the requester to run: graphdeck approval-import {out_file}", file=out)
        else:
            print(payload, file=out)
        return 0

    ex.record_approval(request, approval)
    print(f"{verdict} by {approval.approver}{signed}.", file=out)
    channel = ex.approval_channel()
    if channel is not None:
        from ..compliance.channels import GitChannelError

        try:
            channel.publish_approval(approval)
            print("Decision pushed to the approvals repo.", file=out)
        except GitChannelError as exc:
            print(f"Recorded locally but NOT pushed to the approvals repo: {exc}", file=out)
    for note in ex.last_channel_notes:
        print(f"  {note}", file=out)
    if not reject:
        print(f"The requester can now apply it: graphdeck apply {request.request_id}", file=out)
    return 0


def export_request(
    config: AppConfig, request_id: str, *, out_file: Path | None = None, out: TextIO = sys.stdout
) -> int:
    """Write a change request to a file an approver can sign offline."""
    from ..compliance.approval import ApprovalStore

    request = ApprovalStore(config.approvals_dir).load_request(request_id)
    if request is None:
        print(f"No change request {request_id!r}.", file=out)
        return 1
    payload = json.dumps(request.to_dict(), indent=2, default=str)
    if out_file is None:
        print(payload, file=out)
        return 0
    Path(out_file).write_text(payload + "\n", encoding="utf-8")
    print(f"Change request written: {out_file}", file=out)
    print(f"The approver runs: graphdeck approve --request-file {out_file} --as <name> "
          f"--out approval-{request.request_id}.json", file=out)
    return 0


def import_approval(config: AppConfig, path: Path, *, out: TextIO = sys.stdout) -> int:
    """Import a signed approval produced elsewhere (offline signing, email, chat)."""
    from ..compliance.approval import Approval

    try:
        approval = Approval.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        print(f"Cannot read approval file: {exc}", file=out)
        return 1
    ex = _context(config).executor
    try:
        request = ex.import_approval(approval)
    except ValueError as exc:
        print(f"Refused: {exc}", file=out)
        return 2
    print(f"Imported: {approval.decision} by {approval.approver} for {request.request_id} "
          f"({request.action_name}).", file=out)
    if approval.approved:
        print(f"Apply it with: graphdeck apply {request.request_id}", file=out)
    return 0


def apply_request(config: AppConfig, request_id: str, *, out: TextIO = sys.stdout) -> int:
    """Execute an approved change request through the full write pipeline."""
    import asyncio

    from ..compliance.approval import APPROVED
    from ..compliance.audit import ChangeReason
    from ..core.errors import PipelineError

    ctx = _context(config)
    ex = ctx.executor
    if ex.approval_channel() is not None:
        from ..compliance.channels import GitChannelError

        try:
            note = ex.sync_approval(request_id)
        except GitChannelError as exc:
            note = f"could not check the approvals repo: {exc}"
        if note:
            print(f"  {note}", file=out)
    request = ex.approvals.load_request(request_id)
    if request is None:
        print(f"No change request {request_id!r}.", file=out)
        return 1
    if request.status != APPROVED:
        print(f"Request {request_id} is {request.status}; only approved requests can be applied.",
              file=out)
        return 2
    approval = ex.approvals.load_approval(request_id)
    reason = ChangeReason(**request.reason) if request.reason else None

    async def _run() -> int:
        try:
            if request.is_bulk:
                bulk = await ex.plan_bulk(request.action_id, request.bulk_items)
                results = await ex.commit_bulk(bulk, confirmed=True, reason=reason, approval=approval)
                ok = sum(1 for r in results if r.success)
                print(f"Applied {request.action_name}: {ok}/{len(results)} succeeded.", file=out)
                return 0 if ok == len(results) else 1
            plan = await ex.plan_write(request.action_id, request.params)
            env = await ex.commit_write(plan, confirmed=True, reason=reason, approval=approval)
        except PipelineError as exc:
            print(f"Refused: {exc}", file=out)
            return 2
        if env.success:
            print(f"Applied {request.action_name} (approved by "
                  f"{approval.approver if approval else '?'}).", file=out)
            return 0
        print(f"Execution failed: {env.error_summary()}", file=out)
        return 1

    return asyncio.run(_run())

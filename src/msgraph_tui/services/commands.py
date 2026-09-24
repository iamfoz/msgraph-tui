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


def list_approvals(config: AppConfig, *, show_all: bool = False, out: TextIO = sys.stdout) -> int:
    """List change requests (pending by default)."""
    from ..compliance.approval import PENDING, ApprovalStore

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


def approve(
    config: AppConfig,
    request_id: str,
    approver: str,
    *,
    reject: bool = False,
    comment: str = "",
    out: TextIO = sys.stdout,
) -> int:
    """Record an approve/reject decision by a DIFFERENT person than the requester."""
    from ..compliance.approval import PENDING, make_approval

    ctx = _context(config)
    request = ctx.executor.approvals.load_request(request_id)
    if request is None:
        print(f"No change request {request_id!r}.", file=out)
        return 1
    if request.status != PENDING:
        print(f"Request {request_id} is already {request.status}.", file=out)
        return 2
    print(f"Change request {request.request_id} — {request.action_name} ({request.risk})", file=out)
    print(f"  Requested by: {request.requester}   Tenant: {request.tenant_id}", file=out)
    print(f"  Reason: {request.reason.get('reason', '—')}  Ticket: {request.reason.get('ticket', '—')}", file=out)
    if request.is_bulk:
        print(f"  Bulk: {len(request.bulk_items)} objects", file=out)
    print("  Operation:\n    " + request.preview.replace("\n", "\n    "), file=out)
    try:
        approval = make_approval(
            request, approver, approve=not reject, comment=comment,
            key=config.approval_signing_key(),
        )
    except ValueError as exc:
        print(f"Refused: {exc}", file=out)
        return 2
    ctx.executor.record_approval(request, approval)
    verdict = "REJECTED" if reject else "APPROVED"
    signed = " (signed)" if approval.signature else ""
    print(f"{verdict} by {approval.approver}{signed}.", file=out)
    if not reject:
        print(f"The requester can now apply it: graphdeck apply {request.request_id}", file=out)
    return 0


def apply_request(config: AppConfig, request_id: str, *, out: TextIO = sys.stdout) -> int:
    """Execute an approved change request through the full write pipeline."""
    import asyncio

    from ..compliance.approval import APPROVED
    from ..compliance.audit import ChangeReason
    from ..core.errors import PipelineError

    ctx = _context(config)
    ex = ctx.executor
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

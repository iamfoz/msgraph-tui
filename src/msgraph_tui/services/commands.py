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

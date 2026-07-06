"""Evidence Pack export (PRD §15.4).

Produces an auditor-ready directory (and index manifest) containing the change
history for a period, high-risk register, failures, rollbacks, and the audit
chain integrity verification result. All content is already redacted at
audit-write time; export re-redacts defensively.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..core.export import render
from ..core.redaction import redact
from .audit import AuditLog


def _in_period(entry: dict[str, Any], start: str | None, end: str | None) -> bool:
    ts = entry.get("timestamp", "")
    if start and ts < start:
        return False
    if end and ts > end:
        return False
    return True


def generate_evidence_pack(
    audit_log: AuditLog,
    out_dir: Path,
    *,
    period_start: str | None = None,
    period_end: str | None = None,
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pack_dir = out_dir / f"evidence-pack-{stamp}"
    pack_dir.mkdir(parents=True, exist_ok=True)

    verification = audit_log.verify()
    entries = [
        e for e in audit_log.entries()
        if "_corrupt" not in e and _in_period(e, period_start, period_end)
    ]
    changes = [e for e in entries if e.get("event_type") in ("change", "rollback", "change_intent")]
    high_risk = [e for e in changes if e.get("risk") in ("high", "destructive")]
    failures = [e for e in changes if not e.get("result", {}).get("success", True)]
    rollbacks = [e for e in entries if e.get("event_type") == "rollback"]
    approvals = [
        {"operation_id": e.get("operation_id"), "approval_ref": e["reason"].get("approval_ref"),
         "ticket": e["reason"].get("ticket"), "timestamp": e.get("timestamp")}
        for e in changes
        if e.get("reason", {}).get("approval_ref") or e.get("reason", {}).get("ticket")
    ]
    admins: dict[str, int] = {}
    for e in entries:
        admins[e.get("actor", "unknown")] = admins.get(e.get("actor", "unknown"), 0) + 1

    files: dict[str, Any] = {
        "change-history.jsonl": "\n".join(json.dumps(redact(e), default=str) for e in changes) + "\n",
        "change-history.csv": render(_flatten(changes), "csv"),
        "high-risk-actions.jsonl": "\n".join(json.dumps(redact(e), default=str) for e in high_risk) + "\n",
        "failed-changes.jsonl": "\n".join(json.dumps(redact(e), default=str) for e in failures) + "\n",
        "rollbacks.jsonl": "\n".join(json.dumps(redact(e), default=str) for e in rollbacks) + "\n",
        "approval-references.json": json.dumps(approvals, indent=2, default=str) + "\n",
        "administrator-activity.json": json.dumps(admins, indent=2) + "\n",
        "integrity-verification.json": json.dumps(
            {
                "ok": verification.ok,
                "entries_verified": verification.entries,
                "first_bad_line": verification.first_bad_line,
                "detail": verification.detail,
            },
            indent=2,
        )
        + "\n",
    }

    hashes = {}
    for name, content in files.items():
        (pack_dir / name).write_text(content, encoding="utf-8")
        hashes[name] = hashlib.sha256(content.encode("utf-8")).hexdigest()

    manifest = {
        "tool": "Graphdeck (msgraph-tui)",
        "tool_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period_start": period_start,
        "period_end": period_end,
        "total_events_in_period": len(entries),
        "changes": len(changes),
        "high_risk": len(high_risk),
        "failures": len(failures),
        "rollbacks": len(rollbacks),
        "chain_verification_ok": verification.ok,
        "file_hashes_sha256": hashes,
        "disclaimer": (
            "This pack provides supporting evidence for the organisation's own ISMS. "
            "It does not by itself constitute ISO/IEC 27001 compliance."
        ),
    }
    (pack_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return pack_dir


def _flatten(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for e in entries:
        out.append(
            {
                "timestamp": e.get("timestamp"),
                "event_type": e.get("event_type"),
                "actor": e.get("actor"),
                "tenant_id": e.get("tenant_id"),
                "action_id": e.get("action_id"),
                "operation_id": e.get("operation_id"),
                "provider": e.get("provider"),
                "object_id": e.get("object_id"),
                "object_type": e.get("object_type"),
                "risk": e.get("risk"),
                "success": e.get("result", {}).get("success"),
                "reason": e.get("reason", {}).get("reason"),
                "ticket": e.get("reason", {}).get("ticket"),
                "approval_ref": e.get("reason", {}).get("approval_ref"),
                "rollback_snapshot_id": e.get("rollback_snapshot_id"),
                "rollback_of_operation": e.get("rollback_of_operation"),
                "request_preview": e.get("request_preview"),
            }
        )
    return out

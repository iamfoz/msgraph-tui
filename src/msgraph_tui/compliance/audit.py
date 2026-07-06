"""Tamper-evident audit log (PRD §15).

Append-only JSONL. Each entry embeds the SHA-256 hash of the previous entry
and its own content hash:

    entry_hash = sha256(prev_hash + canonical_json(entry_without_hashes))

verify() re-walks the chain and reports the first divergence. Everything
written here passes through the redaction service first — the audit log must
never contain secrets, tokens, passwords or private key material.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.envelope import utc_now_iso
from ..core.redaction import redact

GENESIS_HASH = "0" * 64


@dataclass
class ChangeReason:
    reason: str = ""
    ticket: str = ""
    requestor: str = ""
    expiry: str = ""            # ISO date if the change is temporary
    approval_ref: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class AuditEvent:
    actor: str
    tenant_id: str
    action_id: str
    operation_id: str
    provider: str
    event_type: str = "change"          # read | change | change_intent | rollback
    object_id: str | None = None
    object_type: str | None = None
    risk: str = "read_only"
    request_preview: str = ""
    before_state: Any = None
    after_state: Any = None
    reason: dict[str, str] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    rollback_snapshot_id: str | None = None
    rollback_of_operation: str | None = None   # links rollback to original op
    interaction_mode: str = "interactive"      # interactive | bulk | scripted | imported
    validation: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=utc_now_iso)

    def to_record(self) -> dict[str, Any]:
        return redact(
            {
                "event_id": self.event_id,
                "timestamp": self.timestamp,
                "event_type": self.event_type,
                "actor": self.actor,
                "tenant_id": self.tenant_id,
                "action_id": self.action_id,
                "operation_id": self.operation_id,
                "provider": self.provider,
                "object_id": self.object_id,
                "object_type": self.object_type,
                "risk": self.risk,
                "request_preview": self.request_preview,
                "before_state": self.before_state,
                "after_state": self.after_state,
                "reason": self.reason,
                "result": self.result,
                "rollback_snapshot_id": self.rollback_snapshot_id,
                "rollback_of_operation": self.rollback_of_operation,
                "interaction_mode": self.interaction_mode,
                "validation": self.validation,
            }
        )


def canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


@dataclass
class VerificationResult:
    ok: bool
    entries: int
    first_bad_line: int | None = None
    detail: str = "chain intact"


class AuditLog:
    """Append-only, hash-chained JSONL audit log."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash = self._recover_last_hash()

    def _recover_last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS_HASH
        last = GENESIS_HASH
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line).get("entry_hash", last)
                except json.JSONDecodeError:
                    continue  # verify() will surface corruption explicitly
        return last

    def append(self, event: AuditEvent) -> dict[str, Any]:
        record = event.to_record()
        prev = self._last_hash
        entry_hash = hashlib.sha256((prev + canonical_json(record)).encode("utf-8")).hexdigest()
        stored = dict(record, prev_hash=prev, entry_hash=entry_hash)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(canonical_json(stored) + "\n")
        self._last_hash = entry_hash
        return stored

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        out.append({"_corrupt": line})
        return out

    def verify(self) -> VerificationResult:
        prev = GENESIS_HASH
        count = 0
        if not self.path.exists():
            return VerificationResult(ok=True, entries=0, detail="no audit log yet")
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                count += 1
                try:
                    stored = json.loads(line)
                except json.JSONDecodeError:
                    return VerificationResult(False, count, lineno, "unparseable entry")
                claimed_prev = stored.get("prev_hash")
                claimed_hash = stored.get("entry_hash")
                record = {
                    k: v for k, v in stored.items() if k not in ("prev_hash", "entry_hash")
                }
                expected = hashlib.sha256(
                    (prev + canonical_json(record)).encode("utf-8")
                ).hexdigest()
                if claimed_prev != prev:
                    return VerificationResult(
                        False, count, lineno, "previous-hash link broken (entry inserted/removed?)"
                    )
                if claimed_hash != expected:
                    return VerificationResult(
                        False, count, lineno, "entry content does not match its hash (tampered?)"
                    )
                prev = claimed_hash
        return VerificationResult(ok=True, entries=count)

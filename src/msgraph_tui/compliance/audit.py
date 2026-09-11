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
import hmac
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.envelope import utc_now_iso
from ..core.redaction import redact

try:  # POSIX advisory file locking for safe concurrent appends
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows fallback (best-effort)
    _HAVE_FCNTL = False

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
    """Append-only, hash-chained JSONL audit log.

    Integrity is layered:

    - **Hash chain** — each entry embeds ``prev_hash`` and
      ``entry_hash = digest(prev_hash + canonical(entry))``. Detects in-place
      edits and reordering.
    - **Head anchor** — a sidecar ``<log>.head`` records the current entry
      count and chain-head hash, updated atomically on each append. ``verify()``
      compares the walked chain against it, so **tail truncation** and
      out-of-band appends are detected (a plain self-contained chain cannot see
      its own tail being cut).
    - **Optional HMAC keying** — pass ``hmac_key`` and entries are signed with
      HMAC-SHA256 instead of a bare hash, so the chain cannot be silently
      reforged without the operator's key. Without a key the log is
      tamper-*evident* (accidental corruption, naive edits), not
      tamper-*proof* against a motivated local admin — see the security model.
    - **Advisory locking** — appends take an exclusive lock so concurrent
      writers cannot interleave and corrupt the chain.
    """

    def __init__(self, path: Path, hmac_key: bytes | None = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._hmac_key = hmac_key
        self._head_path = path.with_name(path.name + ".head")
        self._lock_path = path.with_name(path.name + ".lock")
        self._last_hash, self._count = self._recover_state()

    @property
    def algorithm(self) -> str:
        return "hmac-sha256" if self._hmac_key else "sha256"

    def _digest(self, prev: str, record: dict[str, Any]) -> str:
        payload = (prev + canonical_json(record)).encode("utf-8")
        if self._hmac_key is not None:
            return hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()
        return hashlib.sha256(payload).hexdigest()

    def _recover_state(self) -> tuple[str, int]:
        if not self.path.exists():
            return GENESIS_HASH, 0
        last, count = GENESIS_HASH, 0
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                count += 1
                try:
                    last = json.loads(line).get("entry_hash", last)
                except json.JSONDecodeError:
                    continue  # verify() will surface corruption explicitly
        return last, count

    @contextmanager
    def _exclusive_lock(self):
        """Cross-process advisory lock; best-effort where fcntl is absent."""
        fh = open(self._lock_path, "a+")  # noqa: SIM115 - closed in finally
        try:
            if _HAVE_FCNTL:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if _HAVE_FCNTL:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    def _write_head(self) -> None:
        head = {
            "count": self._count,
            "entry_hash": self._last_hash,
            "algorithm": self.algorithm,
            "updated": utc_now_iso(),
        }
        tmp = self._head_path.with_name(self._head_path.name + ".tmp")
        tmp.write_text(canonical_json(head) + "\n", encoding="utf-8")
        os.replace(tmp, self._head_path)  # atomic

    def _read_head(self) -> dict[str, Any] | None:
        if not self._head_path.exists():
            return None
        try:
            return json.loads(self._head_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def append(self, event: AuditEvent) -> dict[str, Any]:
        record = event.to_record()
        with self._exclusive_lock():
            # Re-read the true tail under the lock so concurrent writers can
            # never both chain off a stale prev_hash.
            self._last_hash, self._count = self._recover_state()
            prev = self._last_hash
            entry_hash = self._digest(prev, record)
            stored = dict(record, prev_hash=prev, entry_hash=entry_hash)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(canonical_json(stored) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._last_hash = entry_hash
            self._count += 1
            self._write_head()
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
        head = self._read_head()
        if not self.path.exists():
            if head and head.get("count", 0) > 0:
                return VerificationResult(
                    False, 0, None,
                    "audit log missing but head anchor records entries (log deleted?)",
                )
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
                expected = self._digest(prev, record)
                if claimed_prev != prev:
                    return VerificationResult(
                        False, count, lineno, "previous-hash link broken (entry inserted/removed?)"
                    )
                if claimed_hash != expected:
                    return VerificationResult(
                        False, count, lineno, "entry content does not match its hash (tampered?)"
                    )
                prev = claimed_hash
        # Compare the walked chain against the out-of-band head anchor. This is
        # what catches tail truncation, which a self-contained chain cannot.
        if head is not None:
            anchored_algo = head.get("algorithm")
            if anchored_algo and anchored_algo != self.algorithm:
                return VerificationResult(
                    False, count, None,
                    f"algorithm mismatch: anchor={anchored_algo}, verifier={self.algorithm} "
                    "(wrong or missing signing key?)",
                )
            if head.get("count") != count:
                return VerificationResult(
                    False, count, None,
                    f"entry count {count} does not match anchored count {head.get('count')} "
                    "(entries truncated or appended out of band?)",
                )
            if head.get("entry_hash") != prev:
                return VerificationResult(
                    False, count, None,
                    "chain head does not match the head anchor (tail rewritten or truncated?)",
                )
            return VerificationResult(ok=True, entries=count, detail="chain intact; matches head anchor")
        return VerificationResult(ok=True, entries=count, detail="chain intact (no head anchor present)")

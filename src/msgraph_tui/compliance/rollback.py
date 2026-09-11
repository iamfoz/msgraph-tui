"""Rollback snapshots and sanity checks (PRD §17).

Rollback is a controlled compensating change, never a blind undo. Snapshots
are captured *before* a write executes; at rollback time the current state is
re-read and compared against both the before- and after-state. If a third
party has changed the object since the original operation, automatic rollback
is blocked and the diff is surfaced.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.actions import RollbackLevel
from ..core.envelope import utc_now_iso
from ..core.redaction import redact


@dataclass
class RollbackSnapshot:
    tenant_id: str
    object_id: str
    object_type: str
    provider: str
    operation_id: str
    action_id: str
    actor: str
    level: str = RollbackLevel.NONE.value
    before_state: dict[str, Any] = field(default_factory=dict)
    after_state: dict[str, Any] = field(default_factory=dict)
    original_params: dict[str, Any] = field(default_factory=dict)
    original_request: str = ""
    inverse_action_id: str | None = None
    inverse_params: dict[str, Any] = field(default_factory=dict)
    inverse_preview: str = ""
    concurrency_marker: str | None = None
    tracked_fields: list[str] = field(default_factory=list)
    non_restorable_fields: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    risk: str = "medium"
    required_checks: list[str] = field(
        default_factory=lambda: ["object_exists", "type_unchanged", "no_drift"]
    )
    consumed: bool = False               # set once a rollback has been executed
    snapshot_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return redact(dict(self.__dict__))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RollbackSnapshot:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class DriftItem:
    field: str
    before: Any
    after: Any    # state right after the original change
    current: Any


@dataclass
class SanityCheckResult:
    can_rollback: bool
    blocked: bool
    warnings: list[str] = field(default_factory=list)
    drift: list[DriftItem] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        if self.blocked:
            return "BLOCKED: " + "; ".join(self.warnings)
        if self.warnings:
            return "Allowed with warnings: " + "; ".join(self.warnings)
        return "All sanity checks passed."


class RollbackStore:
    """One JSON file per snapshot under the snapshots directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, snapshot: RollbackSnapshot) -> Path:
        path = self.directory / f"{snapshot.snapshot_id}.json"
        path.write_text(json.dumps(snapshot.to_dict(), indent=2, default=str), encoding="utf-8")
        return path

    def load(self, snapshot_id: str) -> RollbackSnapshot | None:
        path = self.directory / f"{snapshot_id}.json"
        if not path.exists():
            return None
        try:
            return RollbackSnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            return None

    def mark_consumed(self, snapshot_id: str) -> None:
        snap = self.load(snapshot_id)
        if snap:
            snap.consumed = True
            self.save(snap)

    def all(self) -> list[RollbackSnapshot]:
        snaps = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                snaps.append(RollbackSnapshot.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError):
                continue
        return snaps


def check_rollback_sanity(
    snapshot: RollbackSnapshot,
    current_state: dict[str, Any] | None,
    after_state: dict[str, Any] | None = None,
    *,
    rollback_object_count: int = 1,
    original_object_count: int = 1,
) -> SanityCheckResult:
    """Run the mandatory pre-rollback checks (PRD §17)."""
    warnings: list[str] = []
    checks: dict[str, bool] = {}
    drift: list[DriftItem] = []
    blocked = False

    if snapshot.consumed:
        warnings.append("this snapshot has already been rolled back")
        blocked = True
    checks["not_already_consumed"] = not snapshot.consumed

    if snapshot.level == RollbackLevel.NONE.value:
        warnings.append("no rollback is available for this change")
        blocked = True
    checks["rollback_supported"] = snapshot.level != RollbackLevel.NONE.value

    exists = current_state is not None
    checks["object_exists"] = exists
    if not exists:
        warnings.append("object no longer exists")
        blocked = True
        return SanityCheckResult(False, True, warnings, drift, checks)

    current_type = (current_state or {}).get("@odata.type") or (current_state or {}).get(
        "object_type", snapshot.object_type
    )
    same_type = current_type in (snapshot.object_type, None) or current_type == snapshot.object_type
    checks["type_unchanged"] = same_type
    if not same_type:
        warnings.append(f"object type changed ({snapshot.object_type} -> {current_type})")
        blocked = True

    # Drift detection: for each tracked field, compare after-state (what we left
    # the object as) with current state. If someone changed it since, that is
    # drift and automatic rollback must not proceed silently.
    fields = snapshot.tracked_fields or list(snapshot.before_state.keys())
    # Default to the after-state captured in the snapshot itself.
    reference = after_state if after_state is not None else (snapshot.after_state or None)
    for f in fields:
        before_v = snapshot.before_state.get(f)
        current_v = (current_state or {}).get(f)
        after_v = (reference or {}).get(f) if reference is not None else None
        if reference is not None and after_v != current_v:
            drift.append(DriftItem(f, before_v, after_v, current_v))
    checks["no_drift"] = not drift
    if drift:
        warnings.append(
            "object changed since the original operation on: "
            + ", ".join(d.field for d in drift)
        )
        blocked = True  # policy: block automatic rollback; UI shows diff and
        # requires an explicit diff-confirmed override as a *new* change.

    if snapshot.non_restorable_fields:
        warnings.append(
            "fields not automatically restorable: " + ", ".join(snapshot.non_restorable_fields)
        )
    checks["fully_restorable"] = not snapshot.non_restorable_fields

    blast_ok = rollback_object_count <= original_object_count
    checks["blast_radius_ok"] = blast_ok
    if not blast_ok:
        warnings.append(
            f"rollback would affect {rollback_object_count} objects; original change "
            f"affected {original_object_count}"
        )
        blocked = True

    return SanityCheckResult(can_rollback=not blocked, blocked=blocked, warnings=warnings, drift=drift, checks=checks)


def diff_states(before: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """Field-level diff used by the rollback UI."""
    keys = sorted(set(before) | set(current))
    out = []
    for k in keys:
        b, c = before.get(k), current.get(k)
        if b != c:
            out.append({"field": k, "before": b, "current": c})
    return out

"""The common result envelope returned by every provider engine (PRD §14)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .errors import NormalizedError


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_operation_id() -> str:
    return uuid.uuid4().hex


@dataclass
class RetryInfo:
    attempts: int = 0
    throttled: bool = False
    retry_after_seconds: float | None = None


@dataclass
class PermissionInfo:
    required_scopes: list[str] = field(default_factory=list)
    required_roles: list[str] = field(default_factory=list)
    satisfied: bool | None = None  # None = unknown (e.g. mock mode)


@dataclass
class ResultEnvelope:
    """Uniform result of executing (or previewing/dry-running) an action."""

    success: bool
    provider: str
    action_id: str
    operation_id: str = field(default_factory=new_operation_id)
    # Redacted, human-readable representation of exactly what ran (or would run).
    request_preview: str = ""
    data: Any = None                     # parsed, normalised payload
    raw: Any = None                      # raw provider payload (redacted, where safe)
    warnings: list[str] = field(default_factory=list)
    errors: list[NormalizedError] = field(default_factory=list)
    retry: RetryInfo = field(default_factory=RetryInfo)
    permissions: PermissionInfo = field(default_factory=PermissionInfo)
    audit_metadata: dict[str, Any] = field(default_factory=dict)
    rollback_snapshot_id: str | None = None
    started_at: str = field(default_factory=utc_now_iso)
    duration_ms: float = 0.0
    correlation_id: str | None = None
    page_count: int = 1
    truncated: bool = False
    concurrency_marker: str | None = None  # ETag / lastModified where available

    @property
    def rows(self) -> list[dict]:
        """Data as a list of dicts for table rendering, regardless of shape."""
        if self.data is None:
            return []
        if isinstance(self.data, list):
            return [d if isinstance(d, dict) else {"value": d} for d in self.data]
        if isinstance(self.data, dict):
            return [self.data]
        return [{"value": self.data}]

    def error_summary(self) -> str:
        return "; ".join(e.message for e in self.errors) if self.errors else ""

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "provider": self.provider,
            "action_id": self.action_id,
            "operation_id": self.operation_id,
            "request_preview": self.request_preview,
            "warnings": list(self.warnings),
            "errors": [e.to_dict() for e in self.errors],
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "correlation_id": self.correlation_id,
            "page_count": self.page_count,
            "truncated": self.truncated,
        }


def failure(
    provider: str,
    action_id: str,
    error: NormalizedError,
    *,
    request_preview: str = "",
    raw: Any = None,
) -> ResultEnvelope:
    return ResultEnvelope(
        success=False,
        provider=provider,
        action_id=action_id,
        request_preview=request_preview,
        errors=[error],
        raw=raw,
    )

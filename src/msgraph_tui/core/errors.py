"""Error taxonomy: every provider failure is normalised into one of these categories.

The UI renders NormalizedError instances directly; nothing outside providers/
should need to understand raw Graph OData errors or PowerShell error records.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorCategory(str, Enum):
    AUTH = "auth"                    # not signed in / token invalid
    EXPIRED_SESSION = "expired_session"
    PERMISSION = "permission"        # 401/403, missing scope or role
    THROTTLED = "throttled"          # 429 / server busy
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"            # 409 / concurrency (ETag mismatch)
    INVALID_INPUT = "invalid_input"
    PROVIDER_UNAVAILABLE = "provider_unavailable"  # module missing, engine off
    NETWORK = "network"
    PARSE = "parse"                  # malformed provider output
    UNKNOWN = "unknown"


@dataclass
class NormalizedError:
    category: ErrorCategory
    message: str
    provider_code: str | None = None     # e.g. Graph error code, PS FQErrorId
    status: int | None = None            # HTTP status where applicable
    correlation_id: str | None = None
    retriable: bool = False
    guidance: str | None = None          # human recovery hint
    detail: str | None = None            # redacted raw detail

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "message": self.message,
            "provider_code": self.provider_code,
            "status": self.status,
            "correlation_id": self.correlation_id,
            "retriable": self.retriable,
            "guidance": self.guidance,
            "detail": self.detail,
        }


class GraphdeckError(Exception):
    """Base class for internal (non-provider) errors."""


class RegistryError(GraphdeckError):
    """Invalid action definition or registration conflict."""


class SelectionError(GraphdeckError):
    """No usable provider could be selected for an action."""


class PipelineError(GraphdeckError):
    """Write pipeline invariant violated (e.g. commit without confirmation)."""


GUIDANCE: dict[ErrorCategory, str] = {
    ErrorCategory.AUTH: "Sign in from the Session screen, then retry.",
    ErrorCategory.EXPIRED_SESSION: "Your session has expired. Re-authenticate and retry.",
    ErrorCategory.PERMISSION: (
        "Check the action's required Graph scopes / admin roles (shown in the preview panel) "
        "and ask a Global Administrator to grant them if appropriate."
    ),
    ErrorCategory.THROTTLED: "Microsoft is throttling requests. Graphdeck backed off; retry shortly.",
    ErrorCategory.NOT_FOUND: "The object no longer exists or the identifier is wrong.",
    ErrorCategory.CONFLICT: "The object changed since it was read. Refresh and re-apply the change.",
    ErrorCategory.INVALID_INPUT: "Review the highlighted parameters and correct them.",
    ErrorCategory.PROVIDER_UNAVAILABLE: (
        "The selected provider is not available. Install the required module or choose another "
        "provider in Settings."
    ),
    ErrorCategory.NETWORK: "Network problem reaching Microsoft endpoints. Check connectivity and retry.",
    ErrorCategory.PARSE: "The provider returned output Graphdeck could not parse. See the raw output view.",
    ErrorCategory.UNKNOWN: "Unexpected error. See logs (Ctrl+L) for the redacted raw detail.",
}


def with_guidance(err: NormalizedError) -> NormalizedError:
    if err.guidance is None:
        err.guidance = GUIDANCE.get(err.category)
    return err

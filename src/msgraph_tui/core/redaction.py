"""Central redaction service.

Every string or mapping that crosses an output boundary (previews, logs, audit
events, exports, error details) MUST pass through this module. It combines:

  1. a deny-list of sensitive key names (case-insensitive, substring match), and
  2. pattern scrubbing for token/secret shapes inside free text.

The audit log must never contain secrets — this module is the enforcement point
and is covered by dedicated tests.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***REDACTED***"

# Key names whose values are always redacted wherever they appear in a mapping.
SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "authorization",
    "auth_header",
    "credential",
    "private_key",
    "privatekey",
    "client_assertion",
    "certificate",
    "session_key",
    "cookie",
    "api_key",
    "apikey",
    "passphrase",
)

# Keys that merely *mention* certificates/credentials but hold safe metadata.
SAFE_KEY_EXCEPTIONS: tuple[str, ...] = (
    "credential_expiry",
    "certificate_thumbprint",
    "certificate_expiry",
    "token_expiry",
    "secret_expiry",
    "secret_hint",
    "password_policies",
)

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # JWTs (three base64url segments starting with eyJ)
    re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),
    # Bearer / Basic authorization values
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # key=value style secrets in query strings / connection strings / CLI args
    re.compile(
        r"(?i)\b(client_secret|password|refresh_token|access_token|id_token|sig|sharedaccesskey)"
        r"\s*[=:]\s*[^\s&;\"']{4,}"
    ),
    # PEM blocks
    re.compile(
        r"-----BEGIN [A-Z ]*(PRIVATE KEY|CERTIFICATE)-----.*?-----END [A-Z ]*\1-----",
        re.DOTALL,
    ),
)


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_").replace(" ", "_")
    if any(lowered == exc or lowered.endswith(exc) for exc in SAFE_KEY_EXCEPTIONS):
        return False
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact_text(text: str) -> str:
    """Scrub secret-shaped substrings from free text."""
    if not text:
        return text
    result = text
    for pattern in _PATTERNS:
        if pattern.pattern.startswith("(?i)\\b(client_secret"):
            result = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", result)
        elif pattern.pattern.startswith("(?i)\\b(bearer|basic)"):
            result = pattern.sub(lambda m: f"{m.group(1)} {REDACTED}", result)
        else:
            result = pattern.sub(REDACTED, result)
    return result


def redact(value: Any) -> Any:
    """Recursively redact a JSON-like structure (dicts, lists, strings)."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if is_sensitive_key(str(k)) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value

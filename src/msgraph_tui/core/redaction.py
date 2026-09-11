"""Central redaction service.

Every string or mapping that crosses an output boundary (previews, logs, audit
events, exports, error details) MUST pass through this module. It combines:

  1. a deny-list of sensitive key names (case-insensitive, substring match),
  2. pattern scrubbing for token/secret shapes inside free text, and
  3. scrubbing of secret *values* rendered into command strings (e.g. a
     PowerShell ``-Password 'value'`` parameter), which the key-name and
     ``key=value`` rules would otherwise miss.

The audit log must never contain secrets — this module is the enforcement point
and is covered by dedicated tests.
"""

from __future__ import annotations

import re
from collections.abc import Callable
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
    "accountkey",
    "account_key",
    "signing_key",
    "sas_url",
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

# Secret-shaped keywords used inside key=value / key: value free text. The
# separator tolerates surrounding quotes so dict-repr'd secrets are caught too,
# e.g.  "'refresh_token': '0.AY...'"  and connection-string  "AccountKey=..;".
_KV_KEYWORDS = (
    "client_secret|clientsecret|password|pwd|refresh_token|access_token|id_token"
    "|sig|sharedaccesskey|accountkey|client_assertion|assertion"
    "|api_key|apikey|passphrase"
)

# PowerShell (and CLI) secret parameters: -Password 'value' / -ClientSecret x.
_PS_SECRET_PARAMS = (
    "password|secret|clientsecret|client_secret|token|credential|pwd|passphrase"
    "|assertion|apikey|api_key|accountkey"
)


def _repl_group1_kv(m: re.Match[str]) -> str:
    return f"{m.group(1)}={REDACTED}"


def _repl_group1_space(m: re.Match[str]) -> str:
    return f"{m.group(1)} {REDACTED}"


# Ordered (pattern, replacement) rules applied to every free-text string.
_RULES: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    # JWTs (three base64url segments starting with eyJ)
    (re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), REDACTED),
    # PEM private-key / certificate blocks
    (
        re.compile(
            r"-----BEGIN [A-Z ]*(PRIVATE KEY|CERTIFICATE)-----.*?-----END [A-Z ]*\1-----",
            re.DOTALL,
        ),
        REDACTED,
    ),
    # Bearer / Basic authorization values
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), _repl_group1_space),
    # -SecretParam 'value' | -SecretParam value  (PowerShell / CLI rendered form)
    (
        re.compile(rf"(?i)(-(?:{_PS_SECRET_PARAMS}))\b\s+(?:'[^']*'|\"[^\"]*\"|\S+)"),
        lambda m: f"{m.group(1)} {REDACTED}",
    ),
    # key=value / 'key': 'value' style secrets (query strings, connection
    # strings, dict reprs). Quotes around key and value are tolerated.
    (
        re.compile(
            rf"(?i)\b({_KV_KEYWORDS})['\"]?\s*[=:]\s*['\"]?[^\s,&;'\"]{{4,}}"
        ),
        _repl_group1_kv,
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
    for pattern, repl in _RULES:
        result = pattern.sub(repl, result)
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

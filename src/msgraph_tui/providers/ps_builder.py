"""Pure PowerShell command construction and output parsing — no I/O.

Injection safety: user-supplied values are only ever emitted as single-quoted
PowerShell string literals with embedded single quotes doubled (the only
escape PowerShell honours inside single quotes — no variable expansion, no
subexpressions). Command and parameter *names* come exclusively from the
action registry, never from user input.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.actions import ActionDefinition, PowerShellTemplate

_VALID_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def quote_ps_string(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def format_ps_value(value: Any) -> str:
    if isinstance(value, bool):
        return "$true" if value else "$false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "@(" + ", ".join(format_ps_value(v) for v in value) + ")"
    if value is None:
        return "$null"
    return quote_ps_string(str(value))


def _check_identifier(name: str, kind: str) -> str:
    if not _VALID_IDENTIFIER.match(name):
        raise ValueError(f"Illegal PowerShell {kind} name: {name!r}")
    return name


def build_ps_command(
    action: ActionDefinition,
    params: dict[str, Any],
    *,
    whatif: bool = False,
) -> str:
    """Build the full pipeline text for one action."""
    template: PowerShellTemplate | None = action.powershell
    if template is None:
        raise ValueError(f"{action.id} has no PowerShell template")

    parts: list[str] = [_check_identifier(template.command, "command")]
    for p in template.parameters:
        _check_identifier(p.name, "parameter")
        if p.switch:
            value = params.get(p.source) if p.source else p.literal
            if p.source is None or bool(value):
                parts.append(f"-{p.name}")
            continue
        value = params.get(p.source) if p.source else p.literal
        if value is None:
            continue
        parts.append(f"-{p.name} {format_ps_value(value)}")
    if whatif:
        if not template.supports_whatif:
            raise ValueError(f"{action.id} does not support -WhatIf")
        parts.append("-WhatIf")

    command = " ".join(parts)
    if template.select:
        for col in template.select:
            _check_identifier(col, "property")
        command += " | Select-Object " + ", ".join(template.select)
    command += f" | ConvertTo-Json -Depth {int(template.json_depth)} -Compress"
    return command


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

class PSParseError(ValueError):
    pass


def parse_ps_json_output(stdout: str) -> Any:
    """Extract the JSON document from PowerShell stdout.

    Real-world module output is noisy: banners, progress text and warnings can
    precede (or trail) the JSON emitted by ConvertTo-Json. Strategy: find the
    first line that starts a JSON value and try to decode from each candidate
    position until one parses.
    """
    text = stdout.strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    candidates = [m.start() for m in re.finditer(r"[\[{]", text)]
    # Fast path: whole output is JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for pos in candidates:
        try:
            value, _end = decoder.raw_decode(text[pos:])
            return value
        except json.JSONDecodeError:
            continue
    raise PSParseError(
        "PowerShell output did not contain a parseable JSON document "
        f"(first 200 chars: {text[:200]!r})"
    )


MISSING_MODULE_PATTERNS = (
    "is not recognized as the name of a cmdlet",
    "is not recognized as a name of a cmdlet",
    "could not be loaded because no valid module file was found",
    "No match was found for the specified search criteria and module name",
)

EXPIRED_SESSION_PATTERNS = (
    "authentication needed",
    "token has expired",
    "Connect-MgGraph",
    "Connect-ExchangeOnline",
    "session has expired",
    "InteractionRequired",
)


def classify_ps_stderr(stderr: str) -> str | None:
    """Best-effort classification of PowerShell error text."""
    lowered = stderr.lower()
    if any(p.lower() in lowered for p in MISSING_MODULE_PATTERNS):
        return "missing_module"
    if any(p.lower() in lowered for p in EXPIRED_SESSION_PATTERNS):
        return "expired_session"
    if "access is denied" in lowered or "insufficient privileges" in lowered:
        return "permission"
    if "throttl" in lowered or "too many requests" in lowered:
        return "throttled"
    return None

"""Pure Graph REST request construction — no I/O, fully unit-testable.

Path parameters are URL-quoted per segment; query and body values support
whole-value "{param}" substitution. User input can never change the request
shape, only fill declared slots.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

# A template placeholder: {identifier}. Used for both whole-value substitution
# (the value IS a single placeholder, preserving the param's Python type) and
# embedded substitution inside a larger string (e.g. an OData $filter).
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

from ..core.actions import ActionDefinition, GraphTemplate


@dataclass
class BuiltRequest:
    method: str
    url: str
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    beta: bool = False
    paginate: bool = True

    def preview_text(self) -> str:
        lines = [f"{self.method} {self.url}"]
        for k, v in self.headers.items():
            lines.append(f"{k}: {v}")
        if self.body is not None:
            lines.append("")
            lines.append(json.dumps(self.body, indent=2, ensure_ascii=False, default=str))
        return "\n".join(lines)


class MissingParamError(ValueError):
    pass


def _fill_path(path: str, params: dict[str, Any]) -> str:
    out: list[str] = []
    for segment in path.split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            key = segment[1:-1]
            if key not in params or params[key] in (None, ""):
                raise MissingParamError(f"Missing path parameter {key!r}")
            out.append(quote(str(params[key]), safe=""))
        else:
            out.append(segment)
    return "/".join(out)


_MISSING = object()


def _fill_value(value: Any, params: dict[str, Any]) -> Any:
    """Substitute {param} placeholders in query/body templates.

    Two forms are supported:
      * whole-value  ("{top}")           -> the param's Python value is kept
        (int/list/bool preserved); an absent param resolves to _MISSING so the
        containing dict key / list item is dropped (optional PATCH fields).
      * embedded    ("... eq {sku_id}")  -> each placeholder is replaced
        textually; if any referenced param is absent/None the whole value is
        dropped (_MISSING), never left as a literal "{sku_id}".

    Required params are enforced earlier by ParamSpec, so a dropped required
    filter is not reachable in practice.
    """
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value)
        if whole is not None:
            return params.get(whole.group(1), _MISSING)
        if _PLACEHOLDER.search(value):
            dropped = False

            def _sub(m: re.Match[str]) -> str:
                nonlocal dropped
                v = params.get(m.group(1))
                if v is None:
                    dropped = True
                    return ""
                return str(v)

            filled_text = _PLACEHOLDER.sub(_sub, value)
            return _MISSING if dropped else filled_text
        return value
    if isinstance(value, dict):
        filled = {k: _fill_value(v, params) for k, v in value.items()}
        return {k: v for k, v in filled.items() if v is not _MISSING}
    if isinstance(value, list):
        return [v for v in (_fill_value(v, params) for v in value) if v is not _MISSING]
    return value


def build_graph_request(
    action: ActionDefinition,
    params: dict[str, Any],
    *,
    base_url: str = "https://graph.microsoft.com",
) -> BuiltRequest:
    template = action.graph
    if template is None:
        raise ValueError(f"{action.id} has no Graph template")
    version = "beta" if template.beta else "v1.0"
    path = _fill_path(template.path.lstrip("/"), params)

    query: dict[str, str] = {}
    for key, value in template.query.items():
        filled = _fill_value(value, params)
        if filled is _MISSING or filled in (None, "", []):
            continue
        if isinstance(filled, list):
            filled = ",".join(str(v) for v in filled)
        query[key] = str(filled)

    url = f"{base_url}/{version}/{path}"
    if query:
        url += "?" + urlencode(query, safe="$,()'= ").replace(" ", "%20")

    body = None
    if template.body is not None:
        body = _fill_value(template.body, params)

    headers = {"ConsistencyLevel": "eventual"} if "$count" in query or "$search" in query else {}
    return BuiltRequest(
        method=template.method.upper(),
        url=url,
        body=body,
        headers=headers,
        beta=template.beta,
        paginate=template.paginate and template.method.upper() == "GET",
    )

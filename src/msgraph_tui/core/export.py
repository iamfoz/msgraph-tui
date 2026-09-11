"""Export service: JSON, CSV, Markdown, plain text.

Exports are deliberate user actions. Data passes through redaction before it
is written, and files land in the configured exports directory.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .redaction import redact

FORMATS = ("json", "csv", "md", "txt")


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    cols: list[str] = []
    for row in rows:
        for key in row:
            if key not in cols:
                cols.append(key)
    return cols


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def render(rows: list[dict[str, Any]], fmt: str) -> str:
    rows = [redact(r) for r in rows]
    if fmt == "json":
        return json.dumps(rows, indent=2, ensure_ascii=False, default=str) + "\n"
    cols = _columns(rows)
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _cell(row.get(c)) for c in cols})
        return buf.getvalue()
    if fmt == "md":
        if not rows:
            return "*No rows.*\n"
        lines = [
            "| " + " | ".join(cols) + " |",
            "| " + " | ".join("---" for _ in cols) + " |",
        ]
        for row in rows:
            lines.append(
                "| " + " | ".join(_cell(row.get(c)).replace("|", "\\|") for c in cols) + " |"
            )
        return "\n".join(lines) + "\n"
    if fmt == "txt":
        if not rows:
            return "No rows.\n"
        widths = {c: max(len(c), *(len(_cell(r.get(c))) for r in rows)) for c in cols}
        header = "  ".join(c.ljust(widths[c]) for c in cols)
        sep = "  ".join("-" * widths[c] for c in cols)
        body = [
            "  ".join(_cell(r.get(c)).ljust(widths[c]) for c in cols) for r in rows
        ]
        return "\n".join([header, sep, *body]) + "\n"
    raise ValueError(f"Unknown export format: {fmt} (expected one of {FORMATS})")


def export_rows(
    rows: list[dict[str, Any]],
    fmt: str,
    directory: Path,
    stem: str,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)
    path = directory / f"{safe_stem}-{stamp}.{fmt}"
    path.write_text(render(rows, fmt), encoding="utf-8")
    return path

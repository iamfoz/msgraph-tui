"""Export service: formats, redaction on the way out, file naming."""

import json

from msgraph_tui.core.export import export_rows, render
from msgraph_tui.core.redaction import REDACTED

ROWS = [
    {"id": "u1", "displayName": "Ada", "accountEnabled": True, "mail": None},
    {"id": "u2", "displayName": "Grace | pipe", "accountEnabled": False, "extra": {"a": 1}},
]


def test_json_roundtrip():
    out = json.loads(render(ROWS, "json"))
    assert out[0]["displayName"] == "Ada"
    assert len(out) == 2


def test_csv_headers_union_and_booleans():
    text = render(ROWS, "csv")
    lines = text.strip().splitlines()
    assert lines[0] == "id,displayName,accountEnabled,mail,extra"
    assert "true" in lines[1] and "false" in lines[2]


def test_markdown_escapes_pipes():
    text = render(ROWS, "md")
    assert "Grace \\| pipe" in text
    assert text.startswith("| id |")


def test_txt_alignment():
    text = render(ROWS, "txt")
    header, sep, *rows = text.splitlines()
    assert "displayName" in header and set(sep.replace("  ", " ")) <= {"-", " "}


def test_empty_rows():
    assert render([], "md").strip() == "*No rows.*"
    assert render([], "json").strip() == "[]"


def test_export_redacts_secrets(tmp_path):
    rows = [{"id": "u1", "password": "hunter2"}]
    path = export_rows(rows, "json", tmp_path, "users")
    content = path.read_text()
    assert "hunter2" not in content and REDACTED in content


def test_export_filename_sanitised(tmp_path):
    path = export_rows(ROWS, "csv", tmp_path, "users/../etc naughty")
    assert path.parent == tmp_path
    assert ".." not in path.name and "/" not in path.name.replace(path.suffix, "")

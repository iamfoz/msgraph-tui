"""Headless CLI subcommands (services/commands.py) and audit object-linkage."""

import io
import json
import zipfile

from msgraph_tui.compliance.audit import AuditEvent, AuditLog
from msgraph_tui.core.config import AppConfig, Mode
from msgraph_tui.services import commands


def _cfg(tmp_path) -> AppConfig:
    cfg = AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "state")
    cfg.ensure_dirs()
    return cfg


def _seed_audit(cfg, n=3):
    log = AuditLog(cfg.audit_dir / "audit.jsonl")
    for i in range(n):
        log.append(AuditEvent(actor="a@x", tenant_id="t", action_id=f"act{i}",
                              operation_id=f"op{i}", provider="mock", result={"success": True}))
    return log


def test_verify_audit_intact_and_broken(tmp_path):
    cfg = _cfg(tmp_path)
    out = io.StringIO()
    assert commands.verify_audit(cfg, out=out) == 0
    assert "INTACT" in out.getvalue()

    _seed_audit(cfg, 3)
    out = io.StringIO()
    assert commands.verify_audit(cfg, out=out) == 0

    # tamper: truncate a line's content without rehashing
    path = cfg.audit_dir / "audit.jsonl"
    lines = path.read_text().splitlines()
    lines[1] = lines[1].replace("act1", "HACKED")
    path.write_text("\n".join(lines) + "\n")
    out = io.StringIO()
    assert commands.verify_audit(cfg, out=out) == 1
    assert "BROKEN" in out.getvalue()


def test_evidence_pack_dir_and_zip(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_audit(cfg, 2)
    out = io.StringIO()
    assert commands.evidence_pack(cfg, out=out) == 0
    assert "Evidence pack written" in out.getvalue()

    out = io.StringIO()
    assert commands.evidence_pack(cfg, as_zip=True, out=out) == 0
    zip_line = out.getvalue().strip().split(": ", 1)[1]
    assert zip_line.endswith(".zip")
    with zipfile.ZipFile(zip_line) as zf:
        assert "manifest.json" in zf.namelist()


def test_purge_requires_confirmation_then_removes(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.debug_log_path.write_text("some debug output\n")

    out = io.StringIO()
    assert commands.purge(cfg, logs=True, out=out) == 2  # no --yes
    assert cfg.debug_log_path.exists()

    out = io.StringIO()
    assert commands.purge(cfg, logs=True, assume_yes=True, out=out) == 0
    assert not cfg.debug_log_path.exists()


def test_purge_audit_needs_yes(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_audit(cfg, 1)
    out = io.StringIO()
    # audit requested but no confirmation -> refuses, evidence preserved
    assert commands.purge(cfg, logs=False, audit=True, assume_yes=False, out=out) == 2
    assert (cfg.audit_dir / "audit.jsonl").exists()


def test_purge_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    out = io.StringIO()
    assert commands.purge(cfg, logs=True, out=out) == 0
    assert "Nothing to purge" in out.getvalue()


def test_list_actions_json(tmp_path):
    out = io.StringIO()
    assert commands.list_actions(as_json=True, out=out) == 0
    rows = json.loads(out.getvalue())
    assert len(rows) >= 15
    ids = {r["id"] for r in rows}
    assert {"users.list", "users.update", "groups.list"} <= ids
    # every write action reports its rollback + confirmation in the catalog
    for r in rows:
        assert r["confirmation"] in ("none", "confirm", "typed")


async def test_read_audit_events_have_object_linkage(mock_ctx):
    await mock_ctx.executor.read("users.get", {"user_id": "u-0001"})
    read_events = [e for e in mock_ctx.audit_log.entries() if e["event_type"] == "read"]
    linked = [e for e in read_events if e.get("object_id") == "u-0001"]
    assert linked and linked[-1]["object_type"] == "Entra ID"
